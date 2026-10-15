"""Read-only SSH identity verification for a new VPN node."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh

from app.services.vpn_node_deployment import install_control_known_hosts
from app.services.vpn_node_transport import (
    MAX_PRIVATE_KEY_BYTES,
    VpnNodeTransportError,
    _effective_uid,
    _known_hosts_target,
    _read_private_file,
)

CONNECT_TIMEOUT = 10.0
LOGIN_TIMEOUT = 10.0
PROBE_TIMEOUT = 10.0
MAX_PROBE_OUTPUT_BYTES = 4096

VPN_NODE_TRUST_ERRORS = frozenset(
    {
        "vpn_node_trust_credentials_invalid",
        "vpn_node_trust_connection_failed",
        "vpn_node_trust_host_key_mismatch",
        "vpn_node_trust_environment_invalid",
    }
)

_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
_OPENSSH_ED25519 = re.compile(
    r"ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: ([^\r\n]+))?\Z"
)
_PROBE_KEYS = {
    "version",
    "uid",
    "architecture",
    "host_key",
    "xui_installed",
    "occupied_ports",
}
_SUPPORTED_PORTS = {443, 8443, 2053}

_TRUST_PROBE_SCRIPT = """import json
import os

ports = {443, 8443, 2053}
occupied = set()
for path in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(path, encoding="ascii") as stream:
            lines = stream.read().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        try:
            port = int(fields[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if port in ports:
            occupied.add(port)

with open(
    "/etc/ssh/ssh_host_ed25519_key.pub", encoding="ascii", errors="strict"
) as stream:
    host_key = stream.read().rstrip("\\n")
architecture = {"x86_64": "amd64", "amd64": "amd64"}.get(
    os.uname().machine, os.uname().machine
)
value = {
    "version": 1,
    "uid": os.geteuid(),
    "architecture": architecture,
    "host_key": host_key,
    "xui_installed": os.path.isfile("/usr/local/x-ui/x-ui")
    or os.path.isfile("/etc/systemd/system/x-ui.service"),
    "occupied_ports": sorted(occupied),
}
print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
"""
FIXED_TRUST_COMMAND = "/usr/bin/python3 -I -S -B -c " + shlex.quote(
    _TRUST_PROBE_SCRIPT
)


class VpnNodeTrustError(RuntimeError):
    """A fixed, secret-free classification of a failed trust operation."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VpnNodeTrustProbe:
    known_hosts_line: str = field(repr=False)
    fingerprint: str
    architecture: str
    xui_installed: bool
    occupied_ports: tuple[int, ...]


class _EnvironmentInvalid(Exception):
    pass


def _fail(code: str) -> None:
    error = VpnNodeTrustError(code)
    try:
        raise error from None
    finally:
        error.__context__ = None


def _worker_host_port(worker) -> tuple[str, int]:
    try:
        host = worker.ssh_host or worker.ip_address
        port = 22 if worker.ssh_port is None else worker.ssh_port
        _known_hosts_target(host, port)
        return host, port
    except Exception:  # noqa: BLE001 - worker details never cross this boundary
        _fail("vpn_node_trust_credentials_invalid")


def _connection_options(worker) -> tuple[str, int, dict[str, object]]:
    host, port = _worker_host_port(worker)
    try:
        username = worker.ssh_username
        password = worker.ssh_password
        key_value = worker.ssh_key_path
    except AttributeError:
        _fail("vpn_node_trust_credentials_invalid")
    password_mode = type(password) is str and bool(password)
    key_mode = type(key_value) is str and bool(key_value)
    if username != "root" or password_mode == key_mode:
        _fail("vpn_node_trust_credentials_invalid")

    client_keys: list[object] = []
    if key_mode:
        owner_uid = _effective_uid()
        if owner_uid < 0:
            _fail("vpn_node_trust_credentials_invalid")
        try:
            key_bytes = _read_private_file(
                Path(key_value), limit=MAX_PRIVATE_KEY_BYTES, owner_uid=owner_uid
            )
            client_keys.append(asyncssh.import_private_key(key_bytes))
        except (
            OSError,
            TypeError,
            ValueError,
            asyncssh.KeyImportError,
            VpnNodeTransportError,
        ):
            _fail("vpn_node_trust_credentials_invalid")

    return (
        host,
        port,
        {
            "config": None,
            "known_hosts": None,
            "client_keys": None if password_mode else client_keys,
            "password": password if password_mode else None,
            "preferred_auth": ["password"] if password_mode else ["publickey"],
            "agent_path": None,
            "agent_forwarding": False,
            "pkcs11_provider": None,
            "x509_trusted_certs": None,
            "x509_trusted_cert_paths": [],
            "server_host_key_algs": ["ssh-ed25519"],
            "gss_host": None,
            "gss_kex": False,
            "gss_auth": False,
            "host_based_auth": False,
            "kbdint_auth": False,
            "public_key_auth": key_mode,
            "password_auth": password_mode,
            "disable_trivial_auth": True,
            "connect_timeout": CONNECT_TIMEOUT,
            "login_timeout": LOGIN_TIMEOUT,
        },
    )


async def _bounded_read(reader) -> bytes:
    result = bytearray()
    while True:
        chunk = await reader.read(
            min(65536, MAX_PROBE_OUTPUT_BYTES + 1 - len(result))
        )
        if not isinstance(chunk, bytes):
            raise _EnvironmentInvalid
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > MAX_PROBE_OUTPUT_BYTES:
            raise _EnvironmentInvalid


async def _read_output(process) -> tuple[bytes, bytes]:
    tasks = (
        asyncio.create_task(_bounded_read(process.stdout)),
        asyncio.create_task(_bounded_read(process.stderr)),
    )
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _json_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise _EnvironmentInvalid
        result[key] = value
    return result


def _reject_number(_value: str):
    raise _EnvironmentInvalid


def _parse_public_key(value: str) -> tuple[str, bytes]:
    if type(value) is not str or not value.isascii():
        raise _EnvironmentInvalid
    match = _OPENSSH_ED25519.fullmatch(value)
    if match is None:
        raise _EnvironmentInvalid
    encoded = match.group(1)
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise _EnvironmentInvalid
        asyncssh.import_public_key(f"ssh-ed25519 {encoded}")
    except (ValueError, TypeError, asyncssh.KeyImportError):
        raise _EnvironmentInvalid from None
    return encoded, decoded


def _valid_fingerprint(value: object) -> bool:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        return False
    encoded = value.removeprefix("SHA256:")
    try:
        decoded = base64.b64decode(encoded + "=", validate=True)
    except ValueError:
        return False
    return (
        len(decoded) == hashlib.sha256().digest_size
        and base64.b64encode(decoded).decode("ascii").rstrip("=") == encoded
    )


def _parse_payload(raw: bytes) -> tuple[dict[str, object], str, bytes]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_number,
            parse_float=_reject_number,
            parse_int=int,
        )
        if type(value) is not dict or set(value) != _PROBE_KEYS:
            raise _EnvironmentInvalid
        ports = value["occupied_ports"]
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or type(value["uid"]) is not int
            or value["uid"] != 0
            or value["architecture"] != "amd64"
            or type(value["architecture"]) is not str
            or type(value["xui_installed"]) is not bool
            or type(ports) is not list
            or any(type(port) is not int for port in ports)
            or ports != sorted(ports)
            or len(ports) != len(set(ports))
            or not set(ports).issubset(_SUPPORTED_PORTS)
        ):
            raise _EnvironmentInvalid
        encoded_key, decoded_key = _parse_public_key(value["host_key"])
        canonical = (
            json.dumps(
                {
                    "version": value["version"],
                    "uid": value["uid"],
                    "architecture": value["architecture"],
                    "host_key": value["host_key"],
                    "xui_installed": value["xui_installed"],
                    "occupied_ports": ports,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if raw != canonical:
            raise _EnvironmentInvalid
        return value, encoded_key, decoded_key
    except _EnvironmentInvalid:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        raise _EnvironmentInvalid from None


def _handshake_public_blob(connection) -> bytes:
    try:
        exported = connection.get_server_host_key().export_public_key("openssh")
        if type(exported) is not bytes:
            raise _EnvironmentInvalid
        text = exported.decode("ascii", errors="strict").removesuffix("\n")
        _encoded, decoded = _parse_public_key(text)
        return decoded
    except Exception as exc:
        raise _EnvironmentInvalid from exc


def _close_transport(connection, process) -> None:
    if process is not None:
        try:
            process.close()
        except Exception:  # noqa: BLE001,S110 - best-effort bounded cleanup
            pass
    if connection is not None:
        try:
            connection.abort()
        except Exception:  # noqa: BLE001,S110 - best-effort bounded cleanup
            pass


async def probe_vpn_node_trust(
    worker, *, connector=asyncssh.connect
) -> VpnNodeTrustProbe:
    """Read and verify a node's Ed25519 identity without mutating it."""
    try:
        host, port, options = _connection_options(worker)
    except VpnNodeTrustError:
        raise
    except Exception:  # noqa: BLE001 - credentials never cross this boundary
        _fail("vpn_node_trust_credentials_invalid")
    connection = None
    process = None
    try:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT + LOGIN_TIMEOUT):
                connection = await connector(
                    host,
                    port,
                    username="root",
                    **options,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - collapse connector details
            _fail("vpn_node_trust_connection_failed")

        try:
            handshake_key = _handshake_public_blob(connection)
        except _EnvironmentInvalid:
            _fail("vpn_node_trust_host_key_mismatch")

        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                process = await connection.create_process(
                    FIXED_TRUST_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                stdout, stderr = await _read_output(process)
                await process.wait()
        except asyncio.CancelledError:
            raise
        except _EnvironmentInvalid:
            _fail("vpn_node_trust_environment_invalid")
        except Exception:  # noqa: BLE001 - collapse remote transport details
            _fail("vpn_node_trust_connection_failed")

        if process.exit_status != 0 or stderr:
            _fail("vpn_node_trust_environment_invalid")
        try:
            value, encoded_key, reported_key = _parse_payload(stdout)
        except _EnvironmentInvalid:
            _fail("vpn_node_trust_environment_invalid")
        if handshake_key != reported_key:
            _fail("vpn_node_trust_host_key_mismatch")

        fingerprint = base64.b64encode(hashlib.sha256(reported_key).digest())
        target = _known_hosts_target(host, port)
        return VpnNodeTrustProbe(
            known_hosts_line=f"{target} ssh-ed25519 {encoded_key}",
            fingerprint="SHA256:" + fingerprint.decode("ascii").rstrip("="),
            architecture=value["architecture"],
            xui_installed=value["xui_installed"],
            occupied_ports=tuple(value["occupied_ports"]),
        )
    except asyncio.CancelledError:
        raise
    except VpnNodeTrustError:
        raise
    except Exception:  # noqa: BLE001 - no raw exception may cross this boundary
        _fail("vpn_node_trust_environment_invalid")
    finally:
        _close_transport(connection, process)


async def trust_vpn_node(
    worker,
    expected_fingerprint: str,
    *,
    known_hosts_path: Path,
    reader_gid: int,
    connector=asyncssh.connect,
) -> VpnNodeTrustProbe:
    """Re-probe one node and install its pin only when identity still matches."""
    if not _valid_fingerprint(expected_fingerprint):
        _fail("vpn_node_trust_host_key_mismatch")
    probe = await probe_vpn_node_trust(worker, connector=connector)
    if probe.fingerprint != expected_fingerprint:
        _fail("vpn_node_trust_host_key_mismatch")
    host, port = _worker_host_port(worker)
    try:
        install_control_known_hosts(
            (probe.known_hosts_line + "\n").encode("ascii"),
            target=known_hosts_path,
            host=host,
            port=port,
            reader_gid=reader_gid,
        )
    except VpnNodeTrustError:
        raise
    except Exception:  # noqa: BLE001 - local trust-store details stay private
        _fail("vpn_node_trust_environment_invalid")
    return probe
