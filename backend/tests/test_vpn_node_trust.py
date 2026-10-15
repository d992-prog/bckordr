import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import pytest
from app.services import vpn_node_trust
from app.services.vpn_node_transport import MAX_PRIVATE_KEY_BYTES
from app.services.vpn_node_trust import (
    FIXED_TRUST_COMMAND,
    MAX_PROBE_OUTPUT_BYTES,
    VpnNodeTrustError,
    VpnNodeTrustProbe,
    probe_vpn_node_trust,
    trust_vpn_node,
)

FINGERPRINT_A = "SHA256:" + base64.b64encode(b"a" * 32).decode().rstrip("=")
FINGERPRINT_B = "SHA256:" + base64.b64encode(b"b" * 32).decode().rstrip("=")


class BytesReader:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0
        self.requests: list[int] = []

    async def read(self, size: int) -> bytes:
        self.requests.append(size)
        value = self.value[self.offset : self.offset + size]
        self.offset += len(value)
        return value


class Process:
    def __init__(
        self,
        stdout: bytes,
        *,
        stderr: bytes = b"",
        exit_status: int = 0,
    ) -> None:
        self.stdout = BytesReader(stdout)
        self.stderr = BytesReader(stderr)
        self.exit_status = exit_status
        self.waited = False
        self.closed = False

    async def wait(self) -> None:
        self.waited = True

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self, host_key, process: Process) -> None:
        self.host_key = host_key
        self.process = process
        self.process_calls: list[tuple[str, dict[str, object]]] = []
        self.aborted = False

    def get_server_host_key(self):
        return self.host_key

    async def create_process(self, command: str, **kwargs):
        self.process_calls.append((command, kwargs))
        return self.process

    def abort(self) -> None:
        self.aborted = True


def worker(**overrides):
    values = {
        "ssh_host": "vpn.example",
        "ip_address": "203.0.113.7",
        "ssh_port": 22,
        "ssh_username": "root",
        "ssh_password": "password-secret",
        "ssh_key_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def public_text(key, *, comment: str | None = None) -> str:
    value = key.export_public_key("openssh").decode("ascii").strip()
    return value if comment is None else f"{value} {comment}"


def payload(
    key,
    *,
    uid: int = 0,
    architecture: str = "amd64",
    xui_installed: bool = True,
    occupied_ports: list[int] | None = None,
    comment: str | None = "root@vpn",
) -> bytes:
    value = {
        "version": 1,
        "uid": uid,
        "architecture": architecture,
        "host_key": public_text(key, comment=comment),
        "xui_installed": xui_installed,
        "occupied_ports": [443, 2053]
        if occupied_ports is None
        else occupied_ports,
    }
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        + b"\n"
    )


def connector_for(connection: Connection, calls: list[tuple]) -> object:
    async def connector(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    return connector


@pytest.mark.asyncio
async def test_password_probe_uses_only_strict_unpinned_read_only_connection() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    process = Process(payload(host_key))
    connection = Connection(host_key, process)
    calls: list[tuple] = []

    result = await probe_vpn_node_trust(
        worker(), connector=connector_for(connection, calls)
    )

    encoded_key = public_text(host_key).split()[1]
    decoded_key = base64.b64decode(encoded_key, validate=True)
    fingerprint = base64.b64encode(hashlib.sha256(decoded_key).digest()).decode()
    assert result == VpnNodeTrustProbe(
        known_hosts_line=f"vpn.example ssh-ed25519 {encoded_key}",
        fingerprint=f"SHA256:{fingerprint.rstrip('=')}",
        architecture="amd64",
        xui_installed=True,
        occupied_ports=(443, 2053),
    )
    assert "known_hosts_line" not in repr(result)
    assert calls == [
        (
            ("vpn.example", 22),
            {
                "username": "root",
                "config": None,
                "known_hosts": None,
                "client_keys": None,
                "password": "password-secret",
                "preferred_auth": ["password"],
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
                "public_key_auth": False,
                "password_auth": True,
                "disable_trivial_auth": True,
                "connect_timeout": 10.0,
                "login_timeout": 10.0,
            },
        )
    ]
    assert connection.process_calls == [
        (FIXED_TRUST_COMMAND, {"term_type": None, "encoding": None})
    ]
    assert FIXED_TRUST_COMMAND.startswith("/usr/bin/python3 -I -S -B -c ")
    assert process.waited
    assert connection.aborted


@pytest.mark.asyncio
async def test_key_probe_strictly_reads_and_imports_one_key_for_nondefault_port(
    monkeypatch,
) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    private_bytes = client_key.export_private_key("openssh")
    reads: list[tuple] = []

    def read_private_file(path: Path, *, limit: int, owner_uid: int) -> bytes:
        reads.append((path, limit, owner_uid))
        return private_bytes

    monkeypatch.setattr(vpn_node_trust, "_effective_uid", lambda: 1000)
    monkeypatch.setattr(vpn_node_trust, "_read_private_file", read_private_file)
    connection = Connection(host_key, Process(payload(host_key, occupied_ports=[])))
    calls: list[tuple] = []

    result = await probe_vpn_node_trust(
        worker(
            ssh_host=None,
            ip_address="203.0.113.7",
            ssh_port=2222,
            ssh_password=None,
            ssh_key_path="/run/secrets/node-key",
        ),
        connector=connector_for(connection, calls),
    )

    assert reads == [(Path("/run/secrets/node-key"), MAX_PRIVATE_KEY_BYTES, 1000)]
    options = calls[0][1]
    assert options["password"] is None
    assert options["preferred_auth"] == ["publickey"]
    assert options["public_key_auth"] is True
    assert options["password_auth"] is False
    assert len(options["client_keys"]) == 1
    assert options["client_keys"][0].export_public_key("openssh") == (
        client_key.export_public_key("openssh")
    )
    assert result.known_hosts_line.startswith("[203.0.113.7]:2222 ssh-ed25519 ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"ssh_username": "admin"},
        {"ssh_password": None},
        {"ssh_key_path": "/key"},
        {"ssh_password": 123},
        {"ssh_host": "VPN.EXAMPLE"},
        {"ssh_host": "127.0.0.01"},
        {"ssh_port": 0},
        {"ssh_port": True},
    ],
)
async def test_probe_rejects_invalid_worker_identity_before_connect(overrides) -> None:
    calls = []

    async def connector(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("connector called")

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(worker(**overrides), connector=connector)

    assert caught.value.code == "vpn_node_trust_credentials_invalid"
    assert str(caught.value) == caught.value.code
    assert calls == []


@pytest.mark.asyncio
async def test_probe_rejects_invalid_private_key_without_connecting(monkeypatch) -> None:
    monkeypatch.setattr(vpn_node_trust, "_effective_uid", lambda: 1000)
    monkeypatch.setattr(
        vpn_node_trust, "_read_private_file", lambda *_args, **_kwargs: b"secret"
    )
    calls = []

    async def connector(*args, **kwargs):
        calls.append((args, kwargs))

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(
            worker(ssh_password=None, ssh_key_path="/private/secret"),
            connector=connector,
        )

    assert caught.value.code == "vpn_node_trust_credentials_invalid"
    assert calls == []


@pytest.mark.asyncio
async def test_probe_rejects_handshake_and_reported_host_key_mismatch() -> None:
    handshake_key = asyncssh.generate_private_key("ssh-ed25519")
    reported_key = asyncssh.generate_private_key("ssh-ed25519")
    connection = Connection(handshake_key, Process(payload(reported_key)))

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(
            worker(), connector=connector_for(connection, [])
        )

    assert caught.value.code == "vpn_node_trust_host_key_mismatch"


def malformed_payloads(host_key) -> list[bytes]:
    key = public_text(host_key, comment="root@vpn")
    duplicate = (
        '{"version":1,"version":1,"uid":0,"architecture":"amd64",'
        f'"host_key":{json.dumps(key)},"xui_installed":true,'
        '"occupied_ports":[]}\n'
    ).encode()
    unknown = {
        "version": 1,
        "uid": 0,
        "architecture": "amd64",
        "host_key": key,
        "xui_installed": True,
        "occupied_ports": [],
        "extra": None,
    }
    reordered = {
        "uid": 0,
        "version": 1,
        "architecture": "amd64",
        "host_key": key,
        "xui_installed": True,
        "occupied_ports": [],
    }
    return [
        b"\xff\n",
        b"{}\n",
        duplicate,
        json.dumps(unknown, separators=(",", ":")).encode() + b"\n",
        json.dumps(reordered, separators=(",", ":")).encode() + b"\n",
        payload(host_key) + b"extra",
        payload(host_key, uid=1),
        payload(host_key, architecture="x86_64"),
        payload(host_key, occupied_ports=[443, 443]),
        payload(host_key, occupied_ports=[2053, 443]),
        payload(host_key, occupied_ports=[22]),
    ]


@pytest.mark.asyncio
async def test_probe_rejects_every_noncanonical_environment_payload() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    for raw in malformed_payloads(host_key):
        connection = Connection(host_key, Process(raw))
        with pytest.raises(VpnNodeTrustError) as caught:
            await probe_vpn_node_trust(
                worker(), connector=connector_for(connection, [])
            )
        assert caught.value.code == "vpn_node_trust_environment_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stderr", "exit_status"),
    [(b"remote-secret", 0), (b"", 7)],
)
async def test_probe_rejects_stderr_or_nonzero_exit(stderr, exit_status) -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    connection = Connection(
        host_key,
        Process(payload(host_key), stderr=stderr, exit_status=exit_status),
    )

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(
            worker(), connector=connector_for(connection, [])
        )

    assert caught.value.code == "vpn_node_trust_environment_invalid"


@pytest.mark.asyncio
async def test_probe_bounds_remote_output() -> None:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    process = Process(b"x" * (MAX_PROBE_OUTPUT_BYTES + 1))
    connection = Connection(host_key, process)

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(
            worker(), connector=connector_for(connection, [])
        )

    assert caught.value.code == "vpn_node_trust_environment_invalid"
    assert sum(process.stdout.requests) <= MAX_PROBE_OUTPUT_BYTES + 1


@pytest.mark.asyncio
async def test_probe_times_out_and_aborts_a_hung_remote_read(monkeypatch) -> None:
    started = asyncio.Event()

    class BlockingReader:
        async def read(self, _size: int) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    process = Process(b"")
    process.stdout = BlockingReader()
    connection = Connection(host_key, process)
    monkeypatch.setattr(vpn_node_trust, "PROBE_TIMEOUT", 0.01)

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(
            worker(), connector=connector_for(connection, [])
        )

    assert started.is_set()
    assert caught.value.code == "vpn_node_trust_connection_failed"
    assert connection.aborted


@pytest.mark.asyncio
async def test_probe_propagates_cancellation_and_aborts_connection() -> None:
    started = asyncio.Event()

    class BlockingReader:
        async def read(self, _size: int) -> bytes:
            started.set()
            await asyncio.Event().wait()
            return b""

    host_key = asyncssh.generate_private_key("ssh-ed25519")
    process = Process(b"")
    process.stdout = BlockingReader()
    connection = Connection(host_key, process)
    task = asyncio.create_task(
        probe_vpn_node_trust(worker(), connector=connector_for(connection, []))
    )
    await started.wait()

    task.cancel("caller-cancelled")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert caught.value.args == ("caller-cancelled",)
    assert connection.aborted


@pytest.mark.asyncio
async def test_connection_errors_are_fixed_and_secret_free() -> None:
    secret_values = (
        "vpn.example",
        "password-secret",
        "/private/node-key",
        "stdout-secret",
        FIXED_TRUST_COMMAND,
    )

    async def connector(*_args, **_kwargs):
        raise RuntimeError(" ".join(secret_values))

    with pytest.raises(VpnNodeTrustError) as caught:
        await probe_vpn_node_trust(worker(), connector=connector)

    assert caught.value.code == "vpn_node_trust_connection_failed"
    assert str(caught.value) == "vpn_node_trust_connection_failed"
    for rendered in (str(caught.value), repr(caught.value)):
        assert all(secret not in rendered for secret in secret_values)
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_trust_reprobes_then_installs_the_single_literal_pin(
    monkeypatch, tmp_path
) -> None:
    probe = VpnNodeTrustProbe(
        known_hosts_line="[203.0.113.7]:2222 ssh-ed25519 AAAA",
        fingerprint=FINGERPRINT_A,
        architecture="amd64",
        xui_installed=False,
        occupied_ports=(),
    )
    probes = []
    installs = []

    async def reprobe(value, *, connector):
        probes.append((value, connector))
        return probe

    def install(raw, **kwargs):
        installs.append((raw, kwargs))

    connector = object()
    target = tmp_path / "known_hosts"
    value = worker(ssh_host="203.0.113.7", ssh_port=2222)
    monkeypatch.setattr(vpn_node_trust, "probe_vpn_node_trust", reprobe)
    monkeypatch.setattr(vpn_node_trust, "install_control_known_hosts", install)

    assert await trust_vpn_node(
        value,
        probe.fingerprint,
        known_hosts_path=target,
        reader_gid=44,
        connector=connector,
    ) is probe
    assert probes == [(value, connector)]
    assert installs == [
        (
            (probe.known_hosts_line + "\n").encode("ascii"),
            {
                "target": target,
                "host": "203.0.113.7",
                "port": 2222,
                "reader_gid": 44,
            },
        )
    ]


@pytest.mark.asyncio
async def test_trust_refuses_changed_key_before_any_install(monkeypatch, tmp_path) -> None:
    probes = 0

    async def reprobe(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        return VpnNodeTrustProbe(
            known_hosts_line="vpn.example ssh-ed25519 AAAA",
            fingerprint=FINGERPRINT_B,
            architecture="amd64",
            xui_installed=False,
            occupied_ports=(),
        )

    monkeypatch.setattr(vpn_node_trust, "probe_vpn_node_trust", reprobe)
    monkeypatch.setattr(
        vpn_node_trust,
        "install_control_known_hosts",
        lambda *_args, **_kwargs: pytest.fail("pin installed"),
    )

    with pytest.raises(VpnNodeTrustError) as caught:
        await trust_vpn_node(
            worker(),
            FINGERPRINT_A,
            known_hosts_path=tmp_path / "known_hosts",
            reader_gid=44,
        )

    assert probes == 1
    assert caught.value.code == "vpn_node_trust_host_key_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fingerprint",
    [
        "",
        "sha256:" + "a" * 43,
        "SHA256:" + "=" * 43,
        "SHA256:" + "a" * 43,
        "SHA256:" + "a" * 44,
    ],
)
async def test_trust_validates_expected_fingerprint_before_probe(
    monkeypatch, tmp_path, fingerprint
) -> None:
    monkeypatch.setattr(
        vpn_node_trust,
        "probe_vpn_node_trust",
        lambda *_args, **_kwargs: pytest.fail("probe called"),
    )

    with pytest.raises(VpnNodeTrustError) as caught:
        await trust_vpn_node(
            worker(),
            fingerprint,
            known_hosts_path=tmp_path / "known_hosts",
            reader_gid=44,
        )

    assert caught.value.code == "vpn_node_trust_host_key_mismatch"
