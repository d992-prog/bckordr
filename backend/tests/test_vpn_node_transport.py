from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from uuid import UUID

import asyncssh
import pytest

from app.services import vpn_node_transport
from app.services.vpn_node_transport import (
    FIXED_NODE_COMMAND,
    MAX_STDERR_BYTES,
    NodeControlReceipt,
    VpnNodeTransportError,
    VpnNodeTransportSnapshot,
    _bounded_read,
    _parse_exact_receipt,
    _parse_known_hosts,
    _read_private_file,
    _validate_ancestors,
    execute_vpn_node_request,
    load_transport_snapshot,
)


OPERATION_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DIGEST = "a" * 64
PASSWORD = "synthetic-password-must-not-leak"
VALID_HOST_PIN = b"host " + asyncssh.generate_private_key(
    "ssh-ed25519"
).export_public_key("openssh")


class PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "root" and password == PASSWORD


@pytest.mark.asyncio
async def test_real_loopback_server_uses_exact_pin_fixed_command_and_stdin_only(
    tmp_path,
):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    invocations = []

    async def process_factory(process):
        request = await process.stdin.read()
        invocations.append((process.command, process.term_type, request))
        process.stdout.write(
            json.dumps(
                {
                    "version": 1,
                    "operation_id": str(OPERATION_ID),
                    "request_digest": DIGEST,
                    "state": "observed",
                    "error_code": None,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        line = f"[127.0.0.1]:{port} " + host_key.export_public_key("openssh").decode()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=line.encode(),
            password=PASSWORD,
        )
        receipt = await execute_vpn_node_request(
            snapshot,
            b'{"request":"only-on-stdin"}\n',
            operation_id=OPERATION_ID,
            request_digest=DIGEST,
        )
        assert (receipt.state, receipt.error_code) == ("observed", None)
        assert invocations == [
            (FIXED_NODE_COMMAND, None, b'{"request":"only-on-stdin"}\n')
        ]
        assert "only-on-stdin" not in FIXED_NODE_COMMAND
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_wrong_pin_fails_before_remote_process_reads_stdin():
    server_key = asyncssh.generate_private_key("ssh-ed25519")
    wrong_key = asyncssh.generate_private_key("ssh-ed25519")
    invoked = False

    async def process_factory(process):
        nonlocal invoked
        invoked = True
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[server_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        line = f"[127.0.0.1]:{port} " + wrong_key.export_public_key("openssh").decode()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=line.encode(),
            password=PASSWORD,
        )
        with pytest.raises(VpnNodeTransportError) as caught:
            await execute_vpn_node_request(
                snapshot,
                b"{}\n",
                operation_id=OPERATION_ID,
                request_digest=DIGEST,
            )
        assert caught.value.phase == "preflight"
        assert not invoked
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"*.example.test ssh-ed25519 AAAA",
        b"|1|hash|hash ssh-ed25519 AAAA",
        b"@cert-authority host ssh-ed25519 AAAA",
        b"host,other ssh-ed25519 AAAA",
        b"host ssh-rsa AAAA",
        b"host ssh-ed25519-cert-v01@openssh.com AAAA",
        b"host ssh-ed25519 AAAA",
    ],
)
def test_nonliteral_or_nonport_specific_pins_are_rejected(line):
    with pytest.raises(VpnNodeTransportError):
        _parse_known_hosts(line, "host", 2222)


def test_exact_receipt_rejects_ambiguous_output():
    valid = (
        json.dumps(
            {
                "version": 1,
                "operation_id": str(OPERATION_ID),
                "request_digest": DIGEST,
                "state": "observed",
                "error_code": None,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    assert _parse_exact_receipt(valid, OPERATION_ID, DIGEST).state == "observed"
    for raw in (
        valid + b"{}",
        b" " + valid,
        valid + b"\n",
        valid.replace(b'"version":1', b'"version":1,"version":1'),
        valid.replace(DIGEST.encode(), b"b" * 64),
        valid.replace(b'"state":"observed"', b'"state":"observed","extra":1'),
    ):
        with pytest.raises(VpnNodeTransportError):
            _parse_exact_receipt(raw, OPERATION_ID, DIGEST)


@pytest.mark.asyncio
@pytest.mark.parametrize("password_mode", [True, False])
async def test_connect_options_disable_every_ambient_auth_source(password_mode):
    captured = []
    key = asyncssh.generate_private_key("ssh-ed25519")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    snapshot = VpnNodeTransportSnapshot(
        host="127.0.0.1",
        port=22,
        username="root",
        known_hosts=b"127.0.0.1 " + host_key.export_public_key("openssh"),
        password=PASSWORD if password_mode else None,
        client_key=None if password_mode else key,
    )

    async def connector(*args, **kwargs):
        captured.append((args, kwargs))
        raise OSError("must-not-be-retained")

    with pytest.raises(VpnNodeTransportError):
        await execute_vpn_node_request(
            snapshot,
            b"{}\n",
            operation_id=OPERATION_ID,
            request_digest=DIGEST,
            connector=connector,
        )
    assert len(captured) == 1
    kwargs = captured[0][1]
    assert kwargs["config"] is None
    assert kwargs["agent_path"] is None
    assert kwargs["pkcs11_provider"] is None
    assert kwargs["gss_host"] is None
    assert kwargs["gss_kex"] is kwargs["gss_auth"] is False
    assert kwargs["host_based_auth"] is False
    assert kwargs["kbdint_auth"] is False
    assert kwargs["disable_trivial_auth"] is True
    if password_mode:
        assert kwargs["preferred_auth"] == ["password"]
        assert kwargs["client_keys"] is None
        assert kwargs["public_key_auth"] is False
        assert kwargs["password_auth"] is True
    else:
        assert kwargs["preferred_auth"] == ["publickey"]
        assert kwargs["client_keys"] == [key]
        assert kwargs["password"] is None
        assert kwargs["public_key_auth"] is True
        assert kwargs["password_auth"] is False


def test_snapshot_loader_closes_ambient_auth_and_redacts_secrets(monkeypatch, tmp_path):
    known_hosts_path = tmp_path / "known_hosts"
    key_path = tmp_path / "id_ed25519"
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    private_key = asyncssh.generate_private_key("ssh-ed25519")
    known_hosts_path.write_bytes(
        b"host ssh-ed25519 " + host_key.export_public_key("openssh").split()[1] + b"\n"
    )
    key_path.write_bytes(private_key.export_private_key("openssh"))
    values = {
        known_hosts_path: known_hosts_path.read_bytes(),
        key_path: key_path.read_bytes(),
    }
    monkeypatch.setattr(
        "app.services.vpn_node_transport._read_private_file",
        lambda path, **_kwargs: values[Path(path)],
    )
    monkeypatch.setattr("app.services.vpn_node_transport._effective_uid", lambda: 1000)
    worker = SimpleNamespace(
        ssh_host="host",
        ip_address=None,
        ssh_port=22,
        ssh_username="root",
        ssh_password=None,
        ssh_key_path=str(key_path),
    )
    snapshot = load_transport_snapshot(worker, known_hosts_path)
    rendered = repr(snapshot) + str(snapshot)
    assert str(key_path) not in rendered
    assert private_key.export_private_key("openssh").decode() not in rendered
    assert snapshot.password is None and snapshot.client_key is not None


def test_transport_error_is_secret_free():
    error = VpnNodeTransportError("preflight")
    assert PASSWORD not in repr(error) + str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        VpnNodeTransportSnapshot("", 22, "root", VALID_HOST_PIN, password=PASSWORD),
        VpnNodeTransportSnapshot(
            "host", True, "root", VALID_HOST_PIN, password=PASSWORD
        ),
        VpnNodeTransportSnapshot("host", 0, "root", VALID_HOST_PIN, password=PASSWORD),
        VpnNodeTransportSnapshot(
            "host", 22, "admin", VALID_HOST_PIN, password=PASSWORD
        ),
        VpnNodeTransportSnapshot("host", 22, "root", VALID_HOST_PIN, password=123),
        VpnNodeTransportSnapshot(
            "host", 22, "root", VALID_HOST_PIN, client_key=object()
        ),
    ],
)
async def test_execute_boundary_rejects_forged_snapshot_before_connect(snapshot):
    async def connector(*_args, **_kwargs):
        pytest.fail("connector called")

    with pytest.raises(VpnNodeTransportError):
        await execute_vpn_node_request(
            snapshot,
            b"{}\n",
            operation_id=OPERATION_ID,
            request_digest=DIGEST,
            connector=connector,
        )


def test_stderr_limit_is_finite():
    assert 0 < MAX_STDERR_BYTES <= 1024 * 1024


def metadata(mode, *, uid=1000, size=1, attributes=0):
    return SimpleNamespace(
        st_mode=mode,
        st_uid=uid,
        st_dev=1,
        st_ino=2,
        st_size=size,
        st_mtime_ns=3,
        st_ctime_ns=4,
        st_file_attributes=attributes,
    )


@pytest.mark.parametrize("fault", ["symlink", "owner", "mode", "directory", "reparse"])
def test_private_transport_files_reject_untrusted_metadata(monkeypatch, fault):
    path = Path("C:/veltrix/known_hosts")
    info = metadata(stat.S_IFREG | 0o600)
    if fault == "symlink":
        info.st_mode = stat.S_IFLNK | 0o777
    elif fault == "owner":
        info.st_uid = 1001
    elif fault == "mode":
        info.st_mode = stat.S_IFREG | 0o640
    elif fault == "directory":
        info.st_mode = stat.S_IFDIR | 0o700
    else:
        info.st_file_attributes = 1024
    monkeypatch.setattr(
        "app.services.vpn_node_transport._validate_ancestors", lambda *_args: None
    )
    monkeypatch.setattr(os, "lstat", lambda _path: info)
    with pytest.raises(VpnNodeTransportError):
        _read_private_file(path, limit=100, owner_uid=1000)


def test_private_transport_file_is_nofollow_bounded_and_identity_checked(monkeypatch):
    path = Path("C:/veltrix/known_hosts")
    info = metadata(stat.S_IFREG | 0o600, size=3)
    opened = []
    reads = [b"abc", b""]
    monkeypatch.setattr(
        "app.services.vpn_node_transport._validate_ancestors", lambda *_args: None
    )
    monkeypatch.setattr(os, "lstat", lambda _path: info)
    monkeypatch.setattr(
        os, "open", lambda target, flags: opened.append((target, flags)) or 9
    )
    monkeypatch.setattr(os, "fstat", lambda _fd: info)
    monkeypatch.setattr(os, "read", lambda _fd, _size: reads.pop(0))
    monkeypatch.setattr(os, "close", lambda _fd: None)
    assert _read_private_file(path, limit=3, owner_uid=1000) == b"abc"
    if hasattr(os, "O_NOFOLLOW"):
        assert opened[0][1] & os.O_NOFOLLOW

    reads[:] = [b"abcd"]
    with pytest.raises(VpnNodeTransportError):
        _read_private_file(path, limit=3, owner_uid=1000)


def test_secure_ancestors_allow_only_root_or_service_uid(monkeypatch):
    path = Path("C:/srv/veltrix/known_hosts")
    changed = Path("C:/srv/veltrix")
    for fault in ("symlink", "owner", "writable", "file", "reparse"):

        def lstat(candidate, fault=fault):
            info = metadata(stat.S_IFDIR | 0o755, uid=0)
            if Path(candidate) == changed:
                if fault == "symlink":
                    info.st_mode = stat.S_IFLNK | 0o777
                elif fault == "owner":
                    info.st_uid = 2000
                elif fault == "writable":
                    info.st_mode |= 0o002
                elif fault == "file":
                    info.st_mode = stat.S_IFREG | 0o600
                else:
                    info.st_file_attributes = 1024
            return info

        monkeypatch.setattr(os, "lstat", lstat)
        with pytest.raises(VpnNodeTransportError):
            _validate_ancestors(path, 1000)


@pytest.mark.asyncio
async def test_bounded_reader_rejects_oversized_output():
    class Reader:
        def __init__(self):
            self.chunks = [b"abcd", b"e"]

        async def read(self, _size):
            return self.chunks.pop(0)

    with pytest.raises(VpnNodeTransportError) as caught:
        await _bounded_read(Reader(), 4)
    assert caught.value.phase == "mutation"


@pytest.mark.asyncio
async def test_transport_drains_stdout_and_stderr_concurrently():
    both_started = asyncio.Event()
    started = set()
    valid = (
        json.dumps(
            {
                "version": 1,
                "operation_id": str(OPERATION_ID),
                "request_digest": DIGEST,
                "state": "observed",
                "error_code": None,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )

    class Reader:
        def __init__(self, name, payload):
            self.name = name
            self.payload = payload
            self.done = False

        async def read(self, _size):
            if self.done:
                return b""
            started.add(self.name)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            self.done = True
            return self.payload

    class Stdin:
        def write(self, _value):
            pass

        async def drain(self):
            pass

        def write_eof(self):
            pass

    class Process:
        stdin = Stdin()
        stdout = Reader("stdout", valid)
        stderr = Reader("stderr", b"")
        exit_status = 0

        async def wait(self):
            pass

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def create_process(self, command, **kwargs):
            assert command == FIXED_NODE_COMMAND
            assert kwargs == {"term_type": None, "encoding": None}
            return Process()

    async def connector(*_args, **_kwargs):
        return Connection()

    receipt = await execute_vpn_node_request(
        VpnNodeTransportSnapshot(
            "host",
            22,
            "root",
            b"host "
            + asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh"),
            password=PASSWORD,
        ),
        b"{}\n",
        operation_id=OPERATION_ID,
        request_digest=DIGEST,
        connector=connector,
    )
    assert receipt == NodeControlReceipt("observed", None)
    assert started == {"stdout", "stderr"}


@pytest.mark.asyncio
async def test_real_loopback_rejects_forged_and_ambient_host_pins(
    monkeypatch, tmp_path
):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    wrong_key = asyncssh.generate_private_key("ssh-ed25519")
    invoked = 0

    async def process_factory(process):
        nonlocal invoked
        invoked += 1
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        target = f"[127.0.0.1]:{port}"
        correct = target.encode() + b" " + host_key.export_public_key("openssh")
        hostile_ssh = tmp_path / "hostile-home" / ".ssh"
        hostile_ssh.mkdir(parents=True)
        (hostile_ssh / "known_hosts").write_bytes(correct)
        monkeypatch.setenv("HOME", str(hostile_ssh.parent))
        monkeypatch.setenv("USERPROFILE", str(hostile_ssh.parent))
        monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "hostile-agent.sock"))
        candidates = [
            None,
            (),
            b"",
            b"*.example.test " + host_key.export_public_key("openssh"),
            b"|1|aGFzaA==|aGFzaA== " + host_key.export_public_key("openssh"),
            b"@cert-authority " + correct,
            target.encode() + b" " + wrong_key.export_public_key("openssh"),
        ]
        for candidate in candidates:
            with pytest.raises(VpnNodeTransportError):
                await execute_vpn_node_request(
                    VpnNodeTransportSnapshot(
                        "127.0.0.1",
                        port,
                        "root",
                        candidate,
                        password=PASSWORD,
                    ),
                    b"{}\n",
                    operation_id=OPERATION_ID,
                    request_digest=DIGEST,
                )
        assert invoked == 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_real_loopback_key_auth_uses_only_imported_key():
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    expected_public = client_key.export_public_key("openssh")

    class KeyServer(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return username == "root" and key.export_public_key("openssh") == (
                expected_public
            )

    async def process_factory(process):
        await process.stdin.read()
        process.stdout.write(
            json.dumps(
                {
                    "version": 1,
                    "operation_id": str(OPERATION_ID),
                    "request_digest": DIGEST,
                    "state": "observed",
                    "error_code": None,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=KeyServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            "127.0.0.1",
            port,
            "root",
            f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key("openssh"),
            client_key=client_key,
        )
        assert await execute_vpn_node_request(
            snapshot,
            b"{}\n",
            operation_id=OPERATION_ID,
            request_digest=DIGEST,
        ) == NodeControlReceipt("observed", None)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stall", "expected_phase"),
    [("connect", "preflight"), ("create", "preflight"), ("drain", "mutation")],
)
async def test_fixed_deadlines_classify_prewrite_and_mutation_timeout(
    monkeypatch, stall, expected_phase
):
    monkeypatch.setattr(vpn_node_transport, "CONNECT_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(vpn_node_transport, "LOGIN_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(
        vpn_node_transport, "REMOTE_OPERATION_TIMEOUT", 0.02, raising=False
    )
    pin = b"host " + asyncssh.generate_private_key("ssh-ed25519").export_public_key(
        "openssh"
    )

    class Stdin:
        def write(self, _value):
            pass

        async def drain(self):
            if stall == "drain":
                await asyncio.Event().wait()

        def write_eof(self):
            pass

    class Process:
        stdin = Stdin()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def create_process(self, *_args, **_kwargs):
            if stall == "create":
                await asyncio.Event().wait()
            return Process()

    async def connector(*_args, **_kwargs):
        if stall == "connect":
            await asyncio.Event().wait()
        return Connection()

    with pytest.raises(VpnNodeTransportError) as caught:
        await asyncio.wait_for(
            execute_vpn_node_request(
                VpnNodeTransportSnapshot("host", 22, "root", pin, password=PASSWORD),
                b"{}\n",
                operation_id=OPERATION_ID,
                request_digest=DIGEST,
                connector=connector,
            ),
            0.25,
        )
    assert caught.value.phase == expected_phase


@pytest.mark.asyncio
async def test_remote_operation_deadline_includes_connection_close(monkeypatch):
    monkeypatch.setattr(
        vpn_node_transport, "REMOTE_OPERATION_TIMEOUT", 0.02, raising=False
    )
    pin = b"host " + asyncssh.generate_private_key("ssh-ed25519").export_public_key(
        "openssh"
    )
    receipt = (
        json.dumps(
            {
                "version": 1,
                "operation_id": str(OPERATION_ID),
                "request_digest": DIGEST,
                "state": "observed",
                "error_code": None,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )

    class Reader:
        def __init__(self, value):
            self.value = value

        async def read(self, _size):
            value, self.value = self.value, b""
            return value

    class Stdin:
        def write(self, _value):
            pass

        async def drain(self):
            pass

        def write_eof(self):
            pass

    class Process:
        stdin = Stdin()
        stdout = Reader(receipt)
        stderr = Reader(b"")
        exit_status = 0

        async def wait(self):
            pass

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            await asyncio.Event().wait()

        async def create_process(self, *_args, **_kwargs):
            return Process()

    async def connector(*_args, **_kwargs):
        return Connection()

    with pytest.raises(VpnNodeTransportError) as caught:
        await asyncio.wait_for(
            execute_vpn_node_request(
                VpnNodeTransportSnapshot("host", 22, "root", pin, password=PASSWORD),
                b"{}\n",
                operation_id=OPERATION_ID,
                request_digest=DIGEST,
                connector=connector,
            ),
            0.25,
        )
    assert caught.value.phase == "mutation"


@pytest.mark.asyncio
async def test_oversized_reader_cancels_and_collects_blocked_sibling():
    sibling_cancelled = asyncio.Event()
    release_sibling = asyncio.Event()
    pin = b"host " + asyncssh.generate_private_key("ssh-ed25519").export_public_key(
        "openssh"
    )

    class OversizedReader:
        async def read(self, _size):
            return b"x" * (vpn_node_transport.MAX_STDOUT_BYTES + 1)

    class BlockedReader:
        async def read(self, _size):
            try:
                await release_sibling.wait()
                return b""
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    class Stdin:
        def write(self, _value):
            pass

        async def drain(self):
            pass

        def write_eof(self):
            pass

    class Process:
        stdin = Stdin()
        stdout = OversizedReader()
        stderr = BlockedReader()
        exit_status = 0

        async def wait(self):
            pass

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def create_process(self, *_args, **_kwargs):
            return Process()

    async def connector(*_args, **_kwargs):
        return Connection()

    try:
        with pytest.raises(VpnNodeTransportError) as caught:
            await execute_vpn_node_request(
                VpnNodeTransportSnapshot("host", 22, "root", pin, password=PASSWORD),
                b"{}\n",
                operation_id=OPERATION_ID,
                request_digest=DIGEST,
                connector=connector,
            )
        assert caught.value.phase == "mutation"
        assert sibling_cancelled.is_set()
    finally:
        release_sibling.set()
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("username", "password", "key_path"),
    [
        ("admin", PASSWORD, None),
        ("root", None, None),
        ("root", PASSWORD, "C:/keys/id_ed25519"),
        ("root", 123, None),
        ("root", None, 123),
    ],
)
def test_snapshot_requires_root_and_exactly_one_auth_mode(
    monkeypatch, tmp_path, username, password, key_path
):
    monkeypatch.setattr("app.services.vpn_node_transport._effective_uid", lambda: 1000)
    monkeypatch.setattr(
        "app.services.vpn_node_transport._read_private_file",
        lambda *_args, **_kwargs: pytest.fail("secret file read"),
    )
    worker = SimpleNamespace(
        ssh_host="host",
        ip_address=None,
        ssh_port=22,
        ssh_username=username,
        ssh_password=password,
        ssh_key_path=key_path,
    )
    with pytest.raises(VpnNodeTransportError):
        load_transport_snapshot(worker, tmp_path / "known_hosts")
