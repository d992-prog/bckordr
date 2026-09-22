from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import pytest

from app.services.vpn_node_journal import (
    NodeOperationReceipt,
    initialize_node_journal,
)
from app.services.vpn_node_request import node_request_digest, parse_node_request
from app.services.vpn_xray_runtime import XrayRuntimeObservation
from app.services.vpn_xui_node_executor import execute_node_client_operation


OPERATION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
SECRET = "synthetic-node-token-must-not-leak"


class SyntheticProcessInterruption(BaseException):
    pass


def request_value() -> dict:
    return {
        "version": 1,
        "operation_id": OPERATION_ID,
        "access_key_id": 7,
        "generation": 3,
        "action": "provision",
        "target": {
            "endpoint_id": 1,
            "worker_id": 2,
            "inbound_id": 3,
            "public_host": "vpn.example.test",
            "port": 443,
            "protocol": "vless",
            "transport": "tcp",
            "security": "reality",
            "server_name": "example.test",
            "public_key": PUBLIC_KEY,
            "short_id": "abcd",
            "fingerprint": "chrome",
            "flow": "xtls-rprx-vision",
        },
        "client_uuid": CLIENT_ID,
        "client_email": "client-7@veltrix.test",
        "sub_id": "stable-sub-id",
        "expires_at_ms": 0,
        "traffic_limit_bytes": 1000,
        "created_at_ms": 1234,
        "allow_create": True,
        "allow_shared_restart": False,
    }


def encoded_request(value=None) -> bytes:
    return json.dumps(value or request_value(), separators=(",", ":")).encode()


@pytest.fixture
def entrypoint():
    from app.services import vpn_node_entrypoint

    return vpn_node_entrypoint


def install_trusted_inputs(monkeypatch, entrypoint, tmp_path):
    config = tmp_path / "node.json"
    token = tmp_path / "api-token"
    database = tmp_path / "x-ui.db"
    journal = tmp_path / "journal"
    values = {
        config: json.dumps(
            {
                "version": 1,
                "panel_url": "http://127.0.0.1:2053/secret-base/",
                "database_path": str(database),
            },
            separators=(",", ":"),
        ).encode(),
        token: SECRET.encode(),
    }
    checked = []

    def trusted_read(path, *, limit):
        checked.append((path, limit))
        return values[path]

    monkeypatch.setattr(entrypoint, "_read_private_file", trusted_read)
    monkeypatch.setattr(
        entrypoint,
        "_validate_private_regular_file",
        lambda path: checked.append((path, "database")),
    )
    monkeypatch.setattr(
        entrypoint,
        "_validate_journal",
        lambda path: checked.append((path, "journal")),
    )
    return config, token, database, journal, checked


def run(entrypoint, raw, *, config, token, journal, executor, uid=lambda: 0):
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    code = entrypoint.run_node_entrypoint(
        io.BytesIO(raw),
        stdout,
        stderr,
        config_path=config,
        token_path=token,
        journal_directory=journal,
        effective_uid=uid,
        executor=executor,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_exact_request_and_receipt(monkeypatch, entrypoint, tmp_path):
    config, token, database, journal, checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )
    calls = []

    def executor(request, **kwargs):
        calls.append((request, kwargs))
        with kwargs["panel_factory"]() as panel:
            assert repr(panel) == "NodePanelSession()"
        return NodeOperationReceipt("observed", None)

    class Panel:
        def __init__(self, panel_url, username=None, password=None, *, api_token=None):
            assert panel_url == "http://127.0.0.1:2053/secret-base/"
            assert username is None and password is None and api_token == SECRET

        def __repr__(self):
            return "NodePanelSession()"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(entrypoint, "NodePanelSession", Panel)
    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=executor,
    )

    parsed = parse_node_request(request_value())
    assert code == 0 and stderr == b""
    assert json.loads(stdout) == {
        "version": 1,
        "operation_id": OPERATION_ID,
        "request_digest": node_request_digest(parsed),
        "state": "observed",
        "error_code": None,
    }
    assert stdout.endswith(b"\n") and stdout.count(b"\n") == 1
    assert calls[0][0] == parsed
    assert calls[0][1]["database_path"] == database
    assert calls[0][1]["journal_directory"] == journal
    assert checked == [
        (config, entrypoint.MAX_CONFIG_BYTES),
        (database, "database"),
        (journal, "journal"),
        (token, entrypoint.MAX_TOKEN_BYTES),
    ]
    assert SECRET.encode() not in stdout + stderr


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"{",
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
        b'{"version":Infinity}',
        b'{"version":-Infinity}',
        b'{"version":1e999}',
        b'{"version":1} trailing',
        b'{"version":1,"unknown":true}',
        b'{"version":"\xff"}',
    ],
)
def test_invalid_input_performs_no_privileged_io(
    monkeypatch, entrypoint, tmp_path, raw
):
    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    monkeypatch.setattr(
        entrypoint,
        "_validate_private_regular_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    monkeypatch.setattr(
        entrypoint,
        "_validate_journal",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    uid_calls = []
    code, stdout, stderr = run(
        entrypoint,
        raw,
        config=tmp_path / "node.json",
        token=tmp_path / "token",
        journal=tmp_path / "journal",
        executor=lambda *_args, **_kwargs: pytest.fail("executor called"),
        uid=lambda: uid_calls.append(1) or 0,
    )
    assert code == entrypoint.EXIT_INVALID_REQUEST
    assert stdout == stderr == b""
    assert not touched and not uid_calls


def test_input_is_bounded_before_privileged_io(monkeypatch, entrypoint, tmp_path):
    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    code, stdout, stderr = run(
        entrypoint,
        b" " * (entrypoint.MAX_REQUEST_BYTES + 1),
        config=tmp_path / "node.json",
        token=tmp_path / "token",
        journal=tmp_path / "journal",
        executor=lambda *_args, **_kwargs: pytest.fail("executor called"),
    )
    assert code == entrypoint.EXIT_INVALID_REQUEST
    assert stdout == stderr == b"" and not touched


def test_root_is_required_before_secret_io(monkeypatch, entrypoint, tmp_path):
    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=tmp_path / "node.json",
        token=tmp_path / "token",
        journal=tmp_path / "journal",
        executor=lambda *_args, **_kwargs: pytest.fail("executor called"),
        uid=lambda: 1000,
    )
    assert code == entrypoint.EXIT_FAILURE
    assert stdout == stderr == b"" and not touched


def test_config_is_exact_and_precedes_token(monkeypatch, entrypoint, tmp_path):
    config = tmp_path / "node.json"
    token = tmp_path / "token"
    journal = tmp_path / "journal"
    reads = []

    def trusted_read(path, *, limit):
        reads.append(path)
        if path == config:
            return b'{"version":1,"panel_url":"http://127.0.0.1/","database_path":"/x","extra":1}'
        raise AssertionError("token read")

    monkeypatch.setattr(entrypoint, "_read_private_file", trusted_read)
    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=lambda *_args, **_kwargs: pytest.fail("executor called"),
    )
    assert code == entrypoint.EXIT_FAILURE
    assert stdout == stderr == b"" and reads == [config]


@pytest.mark.parametrize(
    "state,error_code",
    [
        ("failed", "vpn_node_preflight_failed"),
        ("failed", "vpn_node_interrupted_before_mutation"),
        ("uncertain", "vpn_node_mutation_uncertain"),
        ("stale", "vpn_node_operation_stale"),
        ("blocked", "vpn_node_reconciliation_required"),
        ("blocked", "vpn_node_key_revoked"),
    ],
)
def test_allowlisted_receipts(
    monkeypatch, entrypoint, tmp_path, state, error_code
):
    config, token, _database, journal, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )
    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=lambda *_args, **_kwargs: NodeOperationReceipt(state, error_code),
    )
    assert code == 0 and stderr == b""
    assert json.loads(stdout)["state"] == state
    assert json.loads(stdout)["error_code"] == error_code


@pytest.mark.parametrize(
    "receipt",
    [
        NodeOperationReceipt("observed", "vpn_node_preflight_failed"),
        NodeOperationReceipt("failed", None),
        SimpleNamespace(state="observed", error_code=None),
    ],
)
def test_non_allowlisted_or_inexact_receipt_fails_closed(
    monkeypatch, entrypoint, tmp_path, receipt
):
    config, token, _database, journal, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )
    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=lambda *_args, **_kwargs: receipt,
    )
    assert code == entrypoint.EXIT_FAILURE
    assert stdout == stderr == b""


def test_arbitrary_failure_is_secret_free(monkeypatch, entrypoint, tmp_path):
    config, token, _database, journal, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=fail,
    )
    assert code == entrypoint.EXIT_FAILURE
    assert stdout == stderr == b""
    error = entrypoint.NodeEntrypointError()
    assert SECRET not in repr(error) and SECRET not in str(error)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_process_interruptions_are_contained_and_secret_free(
    monkeypatch, entrypoint, tmp_path, interruption, capsys
):
    config, token, _database, journal, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )

    def interrupt(*_args, **_kwargs):
        raise interruption(SECRET + json.dumps(request_value()))

    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=interrupt,
    )
    captured = capsys.readouterr()
    assert code == entrypoint.EXIT_INTERRUPTED
    assert stdout == stderr == b""
    assert captured.out == captured.err == ""


def process_interruptions():
    return [
        SyntheticProcessInterruption(SECRET),
        BaseExceptionGroup(
            SECRET,
            [RuntimeError(SECRET), SyntheticProcessInterruption(SECRET)],
        ),
    ]


@pytest.mark.parametrize("interruption", process_interruptions())
def test_preparse_base_interruptions_are_contained_without_io(
    monkeypatch, entrypoint, tmp_path, interruption, capsys
):
    class InterruptedInput:
        def read(self, _size):
            raise interruption

    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    code = entrypoint.run_node_entrypoint(
        InterruptedInput(),
        stdout,
        stderr,
        config_path=tmp_path / "node.json",
        token_path=tmp_path / "token",
        journal_directory=tmp_path / "journal",
        effective_uid=lambda: 0,
        executor=lambda *_args, **_kwargs: pytest.fail("executor called"),
    )
    captured = capsys.readouterr()
    assert code == entrypoint.EXIT_INTERRUPTED
    assert stdout.getvalue() == stderr.getvalue() == b""
    assert captured.out == captured.err == ""
    assert touched == []


@pytest.mark.parametrize("interruption", process_interruptions())
def test_postparse_base_interruptions_are_contained_and_secret_free(
    monkeypatch, entrypoint, tmp_path, interruption, capsys
):
    config, token, _database, journal, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, tmp_path
    )

    def interrupt(*_args, **_kwargs):
        raise interruption

    code, stdout, stderr = run(
        entrypoint,
        encoded_request(),
        config=config,
        token=token,
        journal=journal,
        executor=interrupt,
    )
    captured = capsys.readouterr()
    assert code == entrypoint.EXIT_INTERRUPTED
    assert stdout == stderr == b""
    assert captured.out == captured.err == ""


def integrated_record():
    return {
        "id": 19,
        "uuid": CLIENT_ID,
        "email": "client-7@veltrix.test",
        "subId": "stable-sub-id",
        "password": SECRET,
        "auth": "",
        "flow": "xtls-rprx-vision",
        "security": "auto",
        "reverse": None,
        "privateKey": "",
        "publicKey": "",
        "allowedIPs": "",
        "preSharedKey": "",
        "keepAlive": 0,
        "forwardedPorts": "",
        "secret": "",
        "adTag": "",
        "limitIp": 1,
        "limitHwid": 0,
        "totalGB": 1000,
        "expiryTime": 0,
        "enable": True,
        "tgId": 0,
        "group": "",
        "comment": SECRET,
        "reset": 0,
        "resetDay": 0,
        "resetMax": 0,
        "trafficReset": "never",
        "trafficResetDay": 1,
        "createdAt": 1234,
        "updatedAt": 0,
        "inboundIds": [3],
    }


@pytest.fixture
def integrated_node(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    initialize_node_journal(journal)
    database = tmp_path / "x-ui.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.execute("INSERT INTO inbounds VALUES (3)")
        connection.execute(
            "CREATE TABLE client_traffics (id,email,up,down,reset_count)"
        )
        connection.execute(
            "INSERT INTO client_traffics VALUES (90,'client-7@veltrix.test',10,20,2)"
        )
    state = {
        "record": integrated_record(),
        "process": 100,
        "writes": [],
        "mark_checks": [],
        "interrupt_after_mutation": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, obj):
            encoded = json.dumps({"success": True, "obj": obj}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer " + SECRET
            record = state["record"]
            if self.path.endswith("/status"):
                obj = {
                    "panelVersion": "3.8.5",
                    "xray": {"state": "running", "errorMsg": ""},
                }
            elif self.path.endswith("/inbounds/list"):
                obj = [
                    {
                        "id": 3,
                        "protocol": "vless",
                        "enable": True,
                        "port": 443,
                        "trafficReset": "never",
                        "settings": {
                            "decryption": "none",
                            "clients": [
                                {
                                    "id": record["uuid"],
                                    "email": record["email"],
                                    "enable": record["enable"],
                                    "flow": record["flow"],
                                }
                            ],
                        },
                        "streamSettings": {
                            "network": "raw",
                            "security": "reality",
                            "realitySettings": {
                                "serverNames": ["example.test"],
                                "shortIds": ["abcd"],
                                "privateKey": SECRET,
                                "settings": {
                                    "publicKey": PUBLIC_KEY,
                                    "fingerprint": "chrome",
                                    "serverName": "",
                                },
                            },
                        },
                    }
                ]
            elif self.path.endswith("/clients/list"):
                obj = [dict(record)]
            else:
                obj = {
                    "client": {
                        key: value
                        for key, value in record.items()
                        if key != "inboundIds"
                    },
                    "inboundIds": record["inboundIds"],
                    "externalLinks": [],
                    "usedTraffic": 0,
                    "tunnelAllowedIPs": {},
                }
            self.respond(obj)

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + SECRET
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            state["writes"].append((self.path, body))
            with sqlite3.connect(journal / "operations.sqlite3") as connection:
                state["mark_checks"].append(
                    connection.execute(
                        "SELECT phase FROM operations WHERE operation_id=?",
                        (OPERATION_ID,),
                    ).fetchone()
                    == ("mutating",)
                )
            assert self.path.endswith("/clients/bulkDisable")
            assert body == {"emails": ["client-7@veltrix.test"]}
            state["record"]["enable"] = False
            state["process"] += 1
            self.respond({"changed": 1})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
    )
    thread.start()
    state.update(
        journal=journal,
        database=database,
        panel_url=f"http://127.0.0.1:{server.server_port}/secret-base/",
    )
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_integrated(monkeypatch, entrypoint, integrated_node):
    request = request_value()
    request.update(
        action="suspend",
        allow_create=False,
        allow_shared_restart=True,
    )
    config = Path("node.json")
    token = Path("api-token")
    values = {
        config: json.dumps(
            {
                "version": 1,
                "panel_url": integrated_node["panel_url"],
                "database_path": str(integrated_node["database"]),
            },
            separators=(",", ":"),
        ).encode(),
        token: SECRET.encode(),
    }
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda path, *, limit: values[path],
    )
    monkeypatch.setattr(
        entrypoint, "_validate_private_regular_file", lambda _path: None
    )
    monkeypatch.setattr(entrypoint, "_validate_journal", lambda _path: None)

    def runtime_reader(**_kwargs):
        if integrated_node["writes"] and integrated_node["interrupt_after_mutation"]:
            raise KeyboardInterrupt(SECRET)
        return XrayRuntimeObservation(
            "matched" if integrated_node["record"]["enable"] else "not_observed",
            integrated_node["process"],
            123,
        )

    def real_executor(parsed, **kwargs):
        return execute_node_client_operation(
            parsed,
            **kwargs,
            runtime_reader=runtime_reader,
        )

    return run(
        entrypoint,
        encoded_request(request),
        config=config,
        token=token,
        journal=integrated_node["journal"],
        executor=real_executor,
    )


def test_real_entrypoint_executor_panel_database_and_journal_integration(
    monkeypatch, entrypoint, integrated_node
):
    code, stdout, stderr = run_integrated(monkeypatch, entrypoint, integrated_node)
    assert code == 0 and stderr == b""
    assert json.loads(stdout)["state"] == "observed"
    assert len(integrated_node["writes"]) == 1
    assert integrated_node["mark_checks"] == [True]
    with sqlite3.connect(
        integrated_node["journal"] / "operations.sqlite3"
    ) as connection:
        assert connection.execute(
            "SELECT phase,error_code FROM operations WHERE operation_id=?",
            (OPERATION_ID,),
        ).fetchone() == ("observed", None)


def test_real_entrypoint_contains_interruption_after_mutation_and_journals_uncertain(
    monkeypatch, entrypoint, integrated_node
):
    integrated_node["interrupt_after_mutation"] = True
    code, stdout, stderr = run_integrated(monkeypatch, entrypoint, integrated_node)
    assert code == entrypoint.EXIT_INTERRUPTED
    assert stdout == stderr == b""
    assert len(integrated_node["writes"]) == 1
    assert integrated_node["mark_checks"] == [True]
    with sqlite3.connect(
        integrated_node["journal"] / "operations.sqlite3"
    ) as connection:
        assert connection.execute(
            "SELECT phase,error_code FROM operations WHERE operation_id=?",
            (OPERATION_ID,),
        ).fetchone() == ("uncertain", "vpn_node_mutation_uncertain")


def metadata(mode, *, uid=0, dev=1, ino=1, size=1, attributes=0):
    return SimpleNamespace(
        st_mode=mode,
        st_uid=uid,
        st_dev=dev,
        st_ino=ino,
        st_size=size,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=attributes,
    )


@pytest.mark.parametrize("fault", ["symlink", "owner", "mode", "directory", "reparse"])
def test_private_file_rejects_untrusted_metadata(monkeypatch, entrypoint, fault):
    path = Path("/var/lib/veltrix-vpn/control-auth/node.json")
    regular = stat.S_IFREG | 0o600
    info = metadata(regular)
    if fault == "symlink":
        info.st_mode = stat.S_IFLNK | 0o777
    elif fault == "owner":
        info.st_uid = 1001
    elif fault == "mode":
        info.st_mode = regular | 0o040
    elif fault == "directory":
        info.st_mode = stat.S_IFDIR | 0o700
    else:
        info.st_file_attributes = 1024
    monkeypatch.setattr(entrypoint, "_validate_ancestors", lambda _path: None)
    monkeypatch.setattr(os, "lstat", lambda _path: info)
    with pytest.raises(entrypoint.NodeEntrypointError):
        entrypoint._read_private_file(path, limit=100)


def test_private_file_is_nofollow_bounded_and_identity_checked(
    monkeypatch, entrypoint
):
    path = Path("/var/lib/veltrix-vpn/control-auth/node.json")
    info = metadata(stat.S_IFREG | 0o600, size=3)
    opened = []
    reads = [b"abc", b""]
    monkeypatch.setattr(entrypoint, "_validate_ancestors", lambda _path: None)
    monkeypatch.setattr(os, "lstat", lambda _path: info)
    monkeypatch.setattr(os, "open", lambda target, flags: opened.append((target, flags)) or 9)
    monkeypatch.setattr(os, "fstat", lambda _fd: info)
    monkeypatch.setattr(os, "read", lambda _fd, _size: reads.pop(0))
    monkeypatch.setattr(os, "close", lambda _fd: None)
    assert entrypoint._read_private_file(path, limit=3) == b"abc"
    assert opened[0][0] == path
    if hasattr(os, "O_NOFOLLOW"):
        assert opened[0][1] & os.O_NOFOLLOW

    reads[:] = [b"abcd"]
    with pytest.raises(entrypoint.NodeEntrypointError):
        entrypoint._read_private_file(path, limit=3)


def test_database_and_journal_require_existing_root_private_objects(
    monkeypatch, entrypoint
):
    database = Path("/var/lib/x-ui/x-ui.db")
    journal = Path("/var/lib/veltrix-vpn/control-journal")
    entries = {
        database: metadata(stat.S_IFREG | 0o600),
        journal: metadata(stat.S_IFDIR | 0o700),
        journal / "gate.sqlite3": metadata(stat.S_IFREG | 0o600, ino=2),
        journal / "operations.sqlite3": metadata(stat.S_IFREG | 0o600, ino=3),
    }
    monkeypatch.setattr(entrypoint, "_validate_ancestors", lambda _path: None)
    monkeypatch.setattr(os, "lstat", lambda path: entries[Path(path)])
    entrypoint._validate_private_regular_file(database)
    entrypoint._validate_journal(journal)

    entries[journal / "operations.sqlite3"].st_uid = 1001
    with pytest.raises(entrypoint.NodeEntrypointError):
        entrypoint._validate_journal(journal)


def test_secure_ancestors_reject_symlinks_wrong_owner_and_writable_dirs(
    monkeypatch, entrypoint
):
    path = Path("/var/lib/veltrix-vpn/control-auth/node.json")
    good = metadata(stat.S_IFDIR | 0o755)
    changed = Path("/var/lib/veltrix-vpn")

    for fault in ("symlink", "owner", "writable", "file", "reparse"):
        def lstat(candidate, fault=fault):
            info = metadata(good.st_mode)
            if Path(candidate) == changed:
                if fault == "symlink":
                    info.st_mode = stat.S_IFLNK | 0o777
                elif fault == "owner":
                    info.st_uid = 1001
                elif fault == "writable":
                    info.st_mode |= 0o002
                elif fault == "file":
                    info.st_mode = stat.S_IFREG | 0o600
                else:
                    info.st_file_attributes = 1024
            return info

        monkeypatch.setattr(os, "lstat", lstat)
        with pytest.raises(entrypoint.NodeEntrypointError):
            entrypoint._validate_ancestors(path)


def test_fixed_node_paths(entrypoint):
    assert entrypoint.NODE_CONFIG_PATH == Path(
        "/var/lib/veltrix-vpn/control-auth/node.json"
    )
    assert entrypoint.NODE_TOKEN_PATH == Path(
        "/var/lib/veltrix-vpn/control-auth/api-token"
    )
    assert entrypoint.NODE_JOURNAL_DIRECTORY == Path(
        "/var/lib/veltrix-vpn/control-journal"
    )
