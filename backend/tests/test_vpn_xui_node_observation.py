from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from traceback import format_exception
from uuid import UUID

import pytest

from app.services.vpn_endpoints import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_xui_node_http import NodePanelError, NodePanelSession

SECRET = "synthetic_observation_secret_never_export"
CLIENT_UUID = UUID("11111111-2222-4333-8444-555555555555")
EMAIL = "synthetic-profile"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def test_node_observation_module_available():
    assert importlib.util.find_spec("app.services.vpn_xui_node_observation") is not None


@pytest.fixture
def observation():
    spec = importlib.util.find_spec("app.services.vpn_xui_node_observation")
    assert spec is not None, "node observation module must exist"
    return importlib.import_module("app.services.vpn_xui_node_observation")


@pytest.fixture
def data(tmp_path):
    database = tmp_path / "panel.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.executemany("INSERT INTO inbounds VALUES (?)", [(2,), (7,)])
    target = VpnEndpointTarget(
        1,
        1,
        2,
        "vpn.example.test",
        443,
        "vless",
        "tcp",
        "reality",
        "example.test",
        PUBLIC_KEY,
        "abcd",
        "chrome",
        "xtls-rprx-vision",
    )
    embedded = {
        "id": str(CLIENT_UUID),
        "email": EMAIL,
        "enable": True,
        "flow": "xtls-rprx-vision",
    }
    reality = {
        "serverNames": ["example.test"],
        "shortIds": ["abcd"],
        "privateKey": SECRET,
        "settings": {
            "publicKey": PUBLIC_KEY,
            "fingerprint": "chrome",
            "serverName": "",
        },
    }
    return {
        "database": database,
        "target": target,
        "status": {
            "success": True,
            "obj": {
                "panelVersion": "3.8.5",
                "xray": {"state": "running", "errorMsg": ""},
            },
        },
        "inbounds": {
            "success": True,
            "obj": [
                {
                    "id": 2,
                    "protocol": "vless",
                    "enable": True,
                    "port": 443,
                    "settings": {"decryption": "none", "clients": [embedded]},
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": reality,
                    },
                },
                {
                    "id": 7,
                    "protocol": "vless",
                    "enable": True,
                    "port": 8443,
                    "settings": {"decryption": "none", "clients": []},
                    "streamSettings": {"network": "raw", "security": "none"},
                },
            ],
        },
        "clients": {
            "success": True,
            "obj": [
                {
                    "id": 19,
                    "uuid": str(CLIENT_UUID),
                    "email": EMAIL,
                    "enable": True,
                    "inboundIds": [2],
                }
            ],
        },
        "events": [],
        "on_clients": None,
    }


@pytest.fixture
def panel(data):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            data["events"].append(("GET", self.path))
            if self.path == "/csrf-token":
                body = {"success": True, "obj": "synthetic-token"}
            else:
                key = {
                    "/panel/api/server/status": "status",
                    "/panel/api/inbounds/list": "inbounds",
                    "/panel/api/clients/list": "clients",
                }[self.path]
                if key == "clients" and data["on_clients"]:
                    data["on_clients"]()
                body = data[key]
            self.respond(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data["events"].append(("POST", self.path))
            assert self.path in ("/login", "/logout")
            self.respond({"success": True, "obj": None})

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        with NodePanelSession(
            f"http://127.0.0.1:{server.server_port}/", "test", SECRET
        ) as session:
            yield session
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def observe(observation, panel, data):
    return observation.observe_node_client(
        panel,
        target=data["target"],
        client_uuid=CLIENT_UUID,
        client_email=EMAIL,
        database_path=data["database"],
    )


def assert_error(observation, panel, data, code, error_type=VpnEndpointError):
    with pytest.raises(error_type) as caught:
        observe(observation, panel, data)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert SECRET not in "".join(format_exception(caught.value))


def test_endpoint_listener_probe_receives_selected_row_and_target(
    observation, panel, data
):
    calls = []

    def listener(row, target):
        calls.append((row, target))
        return True

    result = observation.observe_node_endpoint(
        panel,
        target=data["target"],
        database_path=data["database"],
        listener_probe=listener,
    )
    assert result.listening is True
    assert calls == [(data["inbounds"]["obj"][0], data["target"])]


def test_default_endpoint_listener_uses_observed_bind(observation, data, monkeypatch):
    calls = []
    row = data["inbounds"]["obj"][0]
    row["listen"] = "203.0.113.10"

    def listener(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(observation, "xray_public_listener_is_bound", listener)

    assert observation.xray_endpoint_is_listening(row, data["target"]) is True
    assert calls == [{"port": 443, "listen": "203.0.113.10"}]


def test_complete_observation_is_small_frozen_and_read_only(
    observation, panel, data, capsys, caplog
):
    before = data["database"].read_bytes()
    payloads = deepcopy([data["status"], data["inbounds"], data["clients"]])
    result = observe(observation, panel, data)
    assert asdict(result) == {
        "state": "matched",
        "record_id": 19,
        "enabled": True,
        "transport": "matched",
        "runtime": "running",
    }
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.runtime = "error"
    assert SECRET not in repr(result) + repr(asdict(result))
    assert data["database"].read_bytes() == before
    assert payloads == [data["status"], data["inbounds"], data["clients"]]
    assert data["events"][-3:] == [
        ("GET", "/panel/api/server/status"),
        ("GET", "/panel/api/inbounds/list"),
        ("GET", "/panel/api/clients/list"),
    ]
    assert capsys.readouterr() == ("", "")
    assert caplog.text == ""


@pytest.mark.parametrize("state", ["absent", "disabled", "wrong_identity"])
def test_identity_cases(observation, panel, data, state):
    if state == "absent":
        data["inbounds"]["obj"][0]["settings"]["clients"] = []
        data["clients"]["obj"] = []
        result = observe(observation, panel, data)
        assert (result.state, result.record_id, result.enabled) == (
            "not_observed",
            None,
            None,
        )
    elif state == "disabled":
        data["inbounds"]["obj"][0]["settings"]["clients"][0]["enable"] = False
        data["clients"]["obj"][0]["enable"] = False
        assert observe(observation, panel, data).enabled is False
    else:
        data["clients"]["obj"][0]["email"] = "wrong"
        assert_error(observation, panel, data, "vpn_xui_identity_conflict")


@pytest.mark.parametrize(
    "ids", [[2], [2, 7, 8], [2, 2], [2, True], [2, "7"], [2, 7.0], [2, 0]]
)
def test_api_coverage_must_equal_local_inventory(observation, panel, data, ids):
    data["inbounds"]["obj"] = [
        dict(data["inbounds"]["obj"][0], id=value) for value in ids
    ]
    assert_error(observation, panel, data, "vpn_xui_inventory_incomplete")


def test_database_drift_between_reads_rejects(observation, panel, data):
    def drift():
        with sqlite3.connect(data["database"]) as connection:
            connection.execute("INSERT INTO inbounds VALUES (8)")

    data["on_clients"] = drift
    assert_error(observation, panel, data, "vpn_xui_inventory_incomplete")


def test_wal_database_snapshot_includes_uncheckpointed_rows(observation, data):
    writer = sqlite3.connect(data["database"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
        with closing(
            sqlite3.connect(f"{data['database'].as_uri()}?immutable=1", uri=True)
        ) as stale:
            assert tuple(stale.execute("SELECT id FROM inbounds ORDER BY id")) == (
                (2,),
                (7,),
            )
        assert observation._local_inbound_ids(data["database"]) == (2, 7, 8)
    finally:
        writer.close()


def test_closed_wal_database_without_sidecars_fails_without_creating_files(
    observation, data
):
    database = data["database"]
    with closing(sqlite3.connect(database)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
    assert all(
        not Path(str(database) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    )

    def directory_state():
        parent = os.lstat(database.parent)
        return (
            (parent.st_dev, parent.st_ino, parent.st_mode, parent.st_mtime_ns),
            {
                path.name: (
                    hashlib.sha256(path.read_bytes()).digest(),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                    path.stat().st_mode,
                )
                for path in database.parent.iterdir()
            },
        )

    before = directory_state()
    with pytest.raises(VpnEndpointError) as caught:
        observation._local_inbound_ids(database)
    assert caught.value.code == str(caught.value) == "vpn_xui_inventory_unavailable"
    assert directory_state() == before
    assert all(
        not Path(str(database) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    )


def test_wal_logical_snapshot_over_limit_fails_when_each_file_fits(
    observation, tmp_path, monkeypatch
):
    database = tmp_path / "sized.sqlite"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA page_size=4096")
        writer.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, payload BLOB)")
        writer.execute("INSERT INTO inbounds VALUES (2, zeroblob(20000))")
        writer.commit()
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO inbounds VALUES (7, zeroblob(20000))")
        writer.commit()

        limit = 40_000
        monkeypatch.setattr(observation, "_DATABASE_LIMIT", limit)
        page_size = writer.execute("PRAGMA page_size").fetchone()[0]
        page_count = writer.execute("PRAGMA page_count").fetchone()[0]
        assert page_count * page_size > limit
        sizes = [
            Path(str(database) + suffix).stat().st_size
            for suffix in ("", "-wal", "-shm")
        ]
        assert 100 <= sizes[0] <= limit
        assert all(0 < size <= limit for size in sizes[1:])

        with pytest.raises(VpnEndpointError) as caught:
            observation._local_inbound_ids(database)
        assert caught.value.code == str(caught.value) == "vpn_xui_inventory_unavailable"
    finally:
        writer.close()


@pytest.mark.parametrize("failure", ["symlink", "wrong_owner"])
def test_wal_sidecar_metadata_fails_closed(observation, data, monkeypatch, failure):
    writer = sqlite3.connect(data["database"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
        wal = Path(str(data["database"]) + "-wal")
        assert wal.is_file()
        real_lstat = os.lstat

        def lstat(path):
            info = real_lstat(path)
            if Path(path) == wal:
                values = list(info)
                if failure == "symlink":
                    values[0] = stat.S_IFLNK | 0o777
                else:
                    values[4] = info.st_uid + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(observation.os, "lstat", lstat)
        with pytest.raises(VpnEndpointError) as caught:
            observation._local_inbound_ids(data["database"])
        assert caught.value.code == str(caught.value) == "vpn_xui_inventory_unavailable"
    finally:
        writer.close()


def test_rollback_database_snapshot_does_not_change_directory(observation, data):
    with sqlite3.connect(data["database"]) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    before = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in data["database"].parent.iterdir()
        if path.is_file()
    }

    assert observation._local_inbound_ids(data["database"]) == (2, 7)

    after = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in data["database"].parent.iterdir()
        if path.is_file()
    }
    assert after == before


def test_rollback_vacuum_cannot_expand_backup_beyond_checked_limit(
    observation, tmp_path, monkeypatch
):
    database = tmp_path / "vacuum.sqlite"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA page_size=512")
        writer.execute("CREATE TABLE inbounds (id)")
        writer.executemany("INSERT INTO inbounds VALUES (?)", [(2,), (7,)])
        writer.commit()
        assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert writer.execute("PRAGMA page_size").fetchone() == (512,)
        assert writer.execute("PRAGMA page_count").fetchone()[0] > 0

    limit = 100_000
    assert 100 <= database.stat().st_size < limit
    monkeypatch.setattr(observation, "_DATABASE_LIMIT", limit)
    real_connect = sqlite3.connect
    vacuum_attempted = False
    copied_bytes = []

    class Connection(sqlite3.Connection):
        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.25):
            nonlocal vacuum_attempted
            assert progress is not None
            vacuum_attempted = True
            with closing(real_connect(database, timeout=0)) as writer:
                writer.execute("PRAGMA page_size=65536")
                try:
                    writer.execute("VACUUM")
                except sqlite3.OperationalError as exc:
                    assert "locked" in str(exc).lower()
                    assert self.in_transaction
                else:
                    assert writer.execute("PRAGMA page_size").fetchone() == (65536,)
                    assert (
                        writer.execute("PRAGMA page_count").fetchone()[0] * 65536
                        > limit
                    )

            def record_progress(status, remaining, total):
                actual_page_size = target.execute("PRAGMA page_size").fetchone()[0]
                copied_bytes.append(total * actual_page_size)
                progress(status, remaining, total)

            return super().backup(
                target,
                pages=pages,
                progress=record_progress,
                name=name,
                sleep=sleep,
            )

    def connect(*args, **kwargs):
        return real_connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(observation.sqlite3, "connect", connect)
    try:
        assert observation._local_inbound_ids(database) == (2, 7)
    except VpnEndpointError as exc:
        assert exc.code == str(exc) == "vpn_xui_inventory_unavailable"
    assert vacuum_attempted
    assert all(size <= limit for size in copied_bytes), copied_bytes


def test_rollback_exclusive_lock_fails_before_sqlite_busy_timeout(observation, data):
    database = data["database"]
    locker = sqlite3.connect(database)
    started = threading.Event()
    finished = threading.Event()
    outcomes = []

    def read_locked_database():
        started.set()
        try:
            outcomes.append(observation._local_inbound_ids(database))
        except Exception as exc:
            outcomes.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=read_locked_database)
    completed_while_locked = False
    try:
        assert locker.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        locker.execute("BEGIN EXCLUSIVE")
        assert locker.in_transaction
        with closing(sqlite3.connect(database, timeout=0)) as probe:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                probe.execute("SELECT name FROM sqlite_master").fetchone()
        worker.start()
        assert started.wait(1)
        completed_while_locked = finished.wait(0.5)
    finally:
        locker.rollback()
        locker.close()
        if worker.ident is not None:
            worker.join(3)

    assert not worker.is_alive()
    assert completed_while_locked
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], VpnEndpointError)
    assert outcomes[0].code == str(outcomes[0]) == "vpn_xui_inventory_unavailable"


def test_rollback_journal_fails_closed_without_changes(observation, panel, data):
    Path(str(data["database"]) + "-journal").write_bytes(b"pending")
    before = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in data["database"].parent.iterdir()
        if path.is_file()
    }
    events = list(data["events"])

    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")

    after = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in data["database"].parent.iterdir()
        if path.is_file()
    }
    assert after == before
    assert data["events"] == events


def test_wal_database_backup_deadline_closes_both_connections(
    observation, data, monkeypatch
):
    writer = sqlite3.connect(data["database"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
        real_connect = sqlite3.connect
        opened = []
        closed = []
        backup_returned = False
        expired = False
        calls = 0
        callback_checked_deadline = False

        class Connection(sqlite3.Connection):
            def close(self):
                closed.append(self)
                super().close()

            def backup(
                self, target, *, pages=-1, progress=None, name="main", sleep=0.25
            ):
                nonlocal backup_returned
                assert progress is not None

                def expire_during_backup(status, remaining, total):
                    nonlocal expired, callback_checked_deadline
                    expired = True
                    before = calls
                    try:
                        progress(status, remaining, total)
                    finally:
                        callback_checked_deadline = calls > before

                result = super().backup(
                    target,
                    pages=pages,
                    progress=expire_during_backup,
                    name=name,
                    sleep=sleep,
                )
                backup_returned = True
                return result

        def monotonic():
            nonlocal calls
            calls += 1
            return 3.0 if expired else 0.0

        def connect(*args, **kwargs):
            connection = real_connect(*args, factory=Connection, **kwargs)
            opened.append(connection)
            return connection

        monkeypatch.setattr(
            observation.sqlite3,
            "connect",
            connect,
        )
        monkeypatch.setattr(observation.time, "monotonic", monotonic)
        with pytest.raises(VpnEndpointError) as caught:
            observation._local_inbound_ids(data["database"])
        assert caught.value.code == str(caught.value) == "vpn_xui_inventory_unavailable"
        assert calls >= 3
        assert callback_checked_deadline
        assert not backup_returned
        assert len(opened) == len(closed) == 2
        assert set(opened) == set(closed)
        assert len(set(closed)) == 2
    finally:
        writer.close()


@pytest.mark.parametrize(
    "kind", ["missing", "relative", "string", "directory", "corrupt", "missing_table"]
)
def test_database_failure_precedes_api_and_never_creates(
    observation, panel, data, kind, tmp_path
):
    if kind == "missing":
        data["database"] = tmp_path / "not-created.sqlite"
    elif kind == "relative":
        data["database"] = Path("relative.sqlite")
    elif kind == "string":
        data["database"] = str(data["database"])
    elif kind == "directory":
        data["database"] = tmp_path
    elif kind == "corrupt":
        data["database"].write_text(SECRET)
    else:
        with sqlite3.connect(data["database"]) as connection:
            connection.execute("DROP TABLE inbounds")
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events
    assert not (tmp_path / "not-created.sqlite").exists()


@pytest.mark.parametrize("version", [None, "3.8.4", "3.8.5 ", 3.85])
def test_version_validation_stops_before_inventory(observation, panel, data, version):
    data["status"]["obj"]["panelVersion"] = version
    assert_error(observation, panel, data, "vpn_xui_version_unsupported")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "xray",
    [
        None,
        {},
        {"state": "unknown", "errorMsg": ""},
        {"state": "running", "errorMsg": None},
    ],
)
def test_malformed_runtime_stops_before_inventory(observation, panel, data, xray):
    data["status"]["obj"]["xray"] = xray
    assert_error(observation, panel, data, "vpn_xui_status_invalid")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "state,message,expected",
    [("stop", "", "stop"), ("error", SECRET, "error"), ("running", SECRET, "error")],
)
def test_runtime_is_cached_observation_without_message(
    observation, panel, data, state, message, expected
):
    data["status"]["obj"]["xray"] = {"state": state, "errorMsg": message}
    result = observe(observation, panel, data)
    assert result.runtime == expected
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", True),
        ("port", "443"),
        ("port", 8443),
        ("enable", False),
        ("enable", 1),
        ("streamSettings", None),
    ],
)
def test_transport_shape_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0][field] = value
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "transport,security,flow",
    [
        ("ws", "reality", "xtls-rprx-vision"),
        ("tcp", "tls", ""),
        ("tcp", "none", "vision"),
        ("tcp", "reality", ""),
    ],
)
def test_unsupported_target(observation, panel, data, transport, security, flow):
    data["target"] = replace(
        data["target"], transport=transport, security=security, flow=flow
    )
    assert observe(observation, panel, data).transport == "unsupported"


@pytest.mark.parametrize("legacy_flow", [None, ""])
def test_legacy_and_raw_alias(observation, panel, data, legacy_flow):
    data["target"] = replace(
        data["target"], inbound_id=7, port=8443, security="none", flow=legacy_flow
    )
    embedded = data["inbounds"]["obj"][0]["settings"]["clients"].pop()
    embedded.pop("flow")
    data["inbounds"]["obj"][1]["settings"]["clients"].append(embedded)
    data["clients"]["obj"][0]["inboundIds"] = [7]
    assert observe(observation, panel, data).transport == "matched"


def test_embedded_flow_not_global_override(observation, panel, data):
    data["clients"]["obj"][0]["flow"] = "wrong-global-flow"
    assert observe(observation, panel, data).transport == "matched"
    data["inbounds"]["obj"][0]["settings"]["clients"][0]["flow"] = ""
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("serverNames", []),
        ("serverNames", ["wrong"]),
        ("shortIds", ["ABCD"]),
        ("shortIds", ["abc"]),
        ("privateKey", ""),
        ("privateKey", 1),
        ("settings", None),
    ],
)
def test_reality_shape_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"][field] = value
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("publicKey", PUBLIC_KEY + "="),
        ("fingerprint", "wrong"),
        ("fingerprint", "bad\n"),
        ("serverName", "wrong"),
        ("spiderX", "/unsafe"),
        ("mldsa65Verify", SECRET),
    ],
)
def test_reality_nested_settings_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"]["settings"][
        field
    ] = value
    assert observe(observation, panel, data).transport == "mismatch"


def test_3x_ui_default_nested_spider_path_is_supported(observation, panel, data):
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"]["settings"][
        "spiderX"
    ] = "/"
    assert observe(observation, panel, data).transport == "matched"


@pytest.mark.parametrize(
    "key,value",
    [
        ("tcpSettings", {"header": {"type": "http"}}),
        ("rawSettings", {"acceptProxyProtocol": True}),
    ],
)
def test_unrepresentable_raw_options_reject(observation, panel, data, key, value):
    data["inbounds"]["obj"][0]["streamSettings"][key] = value
    assert observe(observation, panel, data).transport == "mismatch"


def test_panel_error_propagates(observation, panel, data):
    data["inbounds"]["nodePending"] = True
    assert_error(observation, panel, data, "vpn_xui_apply_pending", NodePanelError)


@pytest.mark.parametrize("fingerprint", ["custom fingerprint", " ", "a" * 32])
def test_fingerprint_contract_allows_printable_ascii(
    observation, panel, data, fingerprint
):
    data["target"] = replace(data["target"], fingerprint=fingerprint)
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"]["settings"][
        "fingerprint"
    ] = fingerprint
    assert observe(observation, panel, data).transport == "matched"


@pytest.mark.parametrize(
    "ids",
    [[2, 2], [2, None], [2, "7"], [2, 7.5], [2, 0], [2, -7], list(range(1, 10002))],
)
def test_invalid_or_oversized_local_ids_fail_before_api(observation, panel, data, ids):
    with sqlite3.connect(data["database"]) as connection:
        connection.execute("DELETE FROM inbounds")
        connection.executemany(
            "INSERT INTO inbounds VALUES (?)", [(value,) for value in ids]
        )
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events


def test_local_reads_select_only_ids_and_use_read_only_connection(
    observation, panel, data, monkeypatch
):
    real_connect = sqlite3.connect
    opened, connections, statements, closed, progress, destinations = (
        [],
        [],
        [],
        [],
        [],
        [],
    )
    operations = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

        def set_progress_handler(self, callback, steps):
            progress.append((self, callback, steps))
            super().set_progress_handler(callback, steps)

        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.25):
            operations.append((self, "BACKUP"))
            return super().backup(
                target, pages=pages, progress=progress, name=name, sleep=sleep
            )

    def connect(database, **kwargs):
        opened.append((database, kwargs))
        connection = real_connect(database, factory=Connection, **kwargs)
        connections.append(connection)
        if database == ":memory:":
            destinations.append(connection)

        def trace(statement):
            statements.append((database, statement))
            operations.append((connection, statement))

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(observation.sqlite3, "connect", connect)
    observe(observation, panel, data)
    assert len(opened) == len(connections) == len(closed) == 4
    assert set(connections) == set(closed)
    assert len(set(closed)) == 4
    assert len(destinations) == 2
    assert sum(database == ":memory:" for database, _ in opened) == 2
    assert all(
        kwargs == {"timeout": 2}
        for database, kwargs in opened
        if database == ":memory:"
    )
    assert (
        sum(
            isinstance(database, str)
            and "mode=ro" in database
            and kwargs.get("uri") is True
            for database, kwargs in opened
        )
        == 2
    )
    assert [
        statement for database, statement in statements if database == ":memory:"
    ].count("PRAGMA query_only=ON") == 2
    assert [
        statement for database, statement in statements if database != ":memory:"
    ].count("PRAGMA query_only=ON") == 2
    for source in set(connections) - set(destinations):
        source_operations = [
            statement for owner, statement in operations if owner is source
        ]
        expected = (
            "PRAGMA busy_timeout=0",
            "PRAGMA query_only=ON",
            "BEGIN",
            "PRAGMA page_size",
            "PRAGMA page_count",
            "BACKUP",
        )
        assert all(statement in source_operations for statement in expected), (
            source_operations
        )
        positions = [source_operations.index(statement) for statement in expected]
        assert positions == sorted(positions), source_operations
    assert [
        statement for _, statement in statements if statement.startswith("SELECT")
    ] == ["SELECT id FROM inbounds ORDER BY id LIMIT 10001"] * 2
    destination_progress = [
        (owner, steps) for owner, _, steps in progress if owner in destinations
    ]
    assert len(destination_progress) == 2
    assert {owner for owner, _ in destination_progress} == set(destinations)
    assert all(steps > 0 for _, steps in destination_progress)


def test_sqlite_deadline_error_is_static_and_connection_closes(
    observation, data, monkeypatch
):
    real_connect = sqlite3.connect
    closed = []
    unexpected = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    snapshot = real_connect(":memory:", factory=Connection)
    try:
        snapshot.deserialize(data["database"].read_bytes())

        def connect(*args, **kwargs):
            connection = real_connect(*args, factory=Connection, **kwargs)
            unexpected.append(connection)
            return connection

        monkeypatch.setattr(observation.sqlite3, "connect", connect)
        monkeypatch.setattr(
            observation, "_database_snapshot", lambda _path, _deadline: snapshot
        )
        ticks = iter([0.0])
        times = []

        def monotonic():
            now = next(ticks, 3.0)
            times.append(now)
            return now

        monkeypatch.setattr(observation.time, "monotonic", monotonic)
        with pytest.raises(VpnEndpointError) as caught:
            observation._local_inbound_ids(data["database"])
        assert caught.value.code == str(caught.value) == "vpn_xui_inventory_unavailable"
        assert unexpected == []
        assert times[0] == 0.0 and 3.0 in times
        assert closed == [snapshot]
    finally:
        if snapshot not in closed:
            snapshot.close()
        for connection in unexpected:
            if connection not in closed:
                connection.close()


def test_symlink_database_is_rejected_before_api(observation, panel, data, monkeypatch):
    real_lstat = os.lstat

    def lstat(path):
        info = real_lstat(path)
        if Path(path) == data["database"]:
            values = list(info)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return info

    monkeypatch.setattr(observation.os, "lstat", lstat)
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events


@pytest.mark.parametrize("obj", [None, [], SECRET])
def test_status_object_shape_is_validated_immediately(observation, panel, data, obj):
    data["status"]["obj"] = obj
    assert_error(observation, panel, data, "vpn_xui_status_invalid")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "change,expected",
    [
        ("decryption", "mismatch"),
        ("protocol", "vpn_xui_inbound_unsupported"),
        ("non_target_transport", "matched"),
    ],
)
def test_identity_protocol_defects_and_transport_scope(
    observation, panel, data, change, expected
):
    if change == "decryption":
        data["inbounds"]["obj"][0]["settings"]["decryption"] = "wrong"
    elif change == "protocol":
        data["inbounds"]["obj"][0]["protocol"] = "trojan"
    else:
        data["inbounds"]["obj"][1]["streamSettings"] = SECRET
    if expected.startswith("vpn_"):
        assert_error(observation, panel, data, expected)
    else:
        assert observe(observation, panel, data).transport == expected


@pytest.mark.parametrize(
    "envelope",
    [
        {"success": 1, "obj": []},
        {"success": True, "nodePending": 0, "obj": []},
        {"success": True, "obj": {}},
        {"success": True, "obj": [None]},
    ],
)
def test_coverage_envelope_is_strict_even_without_http_normalization(
    observation, envelope
):
    with pytest.raises(VpnEndpointError, match="^vpn_xui_inventory_incomplete$"):
        observation._covered_rows(envelope, (), ())


def test_types_reexport_same_objects_and_import_without_site_packages(observation):
    from app.services import vpn_endpoint_types, vpn_endpoints

    for name in ("VpnEndpointError", "VpnEndpointTarget", "EndpointOperation"):
        assert getattr(vpn_endpoint_types, name) is getattr(vpn_endpoints, name)
    code = """
import sys, sqlite3, socket, ssl, pathlib, builtins
def forbidden(*args, **kwargs):
    raise AssertionError('import performed I/O')
sqlite3.connect = socket.socket = pathlib.Path.open = builtins.open = forbidden
import app.services.vpn_xui_node_observation
assert not any(name in sys.modules for name in ('sqlalchemy', 'httpx', 'app.core.config', 'app.db.models'))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
