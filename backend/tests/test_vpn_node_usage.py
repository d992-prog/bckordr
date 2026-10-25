from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import pytest

NOW = 1_800_000_000_000
POLL = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
UIDS = ("11111111-2222-4333-8444-555555555555", "22222222-2222-4333-8444-555555555555")


@pytest.fixture
def usage():
    return importlib.import_module("app.services.vpn_node_usage")


@pytest.fixture
def entry():
    return importlib.import_module("app.services.vpn_node_usage_entrypoint")


def request(usage):
    return usage.UsageRequest(
        1,
        POLL,
        NOW,
        tuple(
            usage.UsageIdentity(index + 1, 7, uid, f"managed-{index}@example.test")
            for index, uid in enumerate(UIDS)
        ),
    )


def receipt(usage):
    return usage.UsageReceipt(
        1,
        POLL,
        NOW,
        NOW + 10,
        "a" * 64,
        tuple(
            usage.KeyUsage(index, "ok", index + 90, 2, 100, 200, "unsupported", ())
            for index in (1, 2)
        ),
    )


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_protocol_roundtrip_and_secret_free_reprs(usage):
    req, result = request(usage), receipt(usage)
    assert (
        usage.parse_node_usage_request(
            usage.serialize_node_usage_request(req, now_ms=NOW), now_ms=NOW
        )
        == req
    )
    assert (
        usage.parse_node_usage_receipt(
            usage.serialize_node_usage_receipt(result, req, now_ms=NOW), req, now_ms=NOW
        )
        == result
    )
    assert UIDS[0] not in repr(req)
    assert "managed-0" not in repr(req)
    assert "203.0.113.1" not in repr(replace(result.keys[0], sources=("203.0.113.1",)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("minute_ms", True),
        ("minute_ms", NOW + 1),
        ("minute_ms", NOW + 60_000),
        ("poll_id", POLL.upper()),
        ("poll_id", "bad"),
    ],
)
def test_invalid_request_fields(usage, field, value):
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_request(
            replace(request(usage), **{field: value}), now_ms=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_key_id", True),
        ("access_key_id", 0),
        ("access_key_id", 2**63),
        ("inbound_id", False),
        ("client_uuid", "ABCDEFAB-2222-4333-8444-555555555555"),
        ("client_uuid", "bad"),
        ("client_email", "bad/email"),
        ("client_email", "x" * 65),
    ],
)
def test_invalid_identity_fields(usage, field, value):
    req = request(usage)
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_request(
            replace(req, identities=(replace(req.identities[0], **{field: value}),)),
            now_ms=NOW,
        )


@pytest.mark.parametrize(
    "kind", ["key", "email", "uuid", "normalized_uuid", "overflow"]
)
def test_duplicate_identity_and_batch_bounds(usage, kind):
    req = request(usage)
    first, second = req.identities
    changes = {
        "key": {"access_key_id": first.access_key_id},
        "email": {"client_email": first.client_email.upper()},
        "uuid": {"client_uuid": first.client_uuid},
        "normalized_uuid": {"client_uuid": "11111111-2222-ffff-8444-555555555555"},
    }
    identities = (
        (first,) * 2049
        if kind == "overflow"
        else (first, replace(second, **changes[kind]))
    )
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_request(
            replace(req, identities=identities), now_ms=NOW
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"raw":"203.0.113.1"}',
        b'{"a":NaN}',
        b"[]",
        b"null",
        b"\xff",
        b"x" * (512 * 1024 + 1),
    ],
    ids=["duplicate", "private", "nan", "list", "null", "encoding", "oversize"],
)
def test_parser_errors_clear_secret_exception_context(usage, raw):
    try:
        raise ValueError("private@example.test 203.0.113.1")
    except ValueError:
        with pytest.raises(usage.VpnNodeUsageError) as caught:
            usage.parse_node_usage_request(raw, now_ms=NOW)
    assert caught.value.__context__ is caught.value.__cause__ is None
    assert "203.0.113.1" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "changes",
    [
        {"record_id": True},
        {"record_id": 0},
        {"reset_count": -1},
        {"reset_count": True},
        {"upload_bytes": -1},
        {"download_bytes": 2**63},
        {"upload_bytes": 1.0},
        {"traffic_state": "unavailable"},
        {"source_state": "unsupported", "sources": ("203.0.113.1",)},
        {"source_state": "ok", "sources": tuple(f"192.0.2.{i}" for i in range(65))},
        {"source_state": "ok", "sources": ("host:80",)},
    ],
)
def test_invalid_key_receipts(usage, changes):
    result = receipt(usage)
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_receipt(
            replace(result, keys=(replace(result.keys[0], **changes), result.keys[1])),
            request(usage),
            now_ms=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"keys": ()},
        {"poll_id": "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        {"observed_ms": True},
        {"observed_ms": NOW + 30_001},
        {"counter_epoch": "A" * 64},
    ],
)
def test_receipt_binds_exact_request_and_time(usage, changes):
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_receipt(
            replace(receipt(usage), **changes), request(usage), now_ms=NOW
        )
    with pytest.raises(usage.VpnNodeUsageError):
        usage.parse_node_usage_receipt(
            b"x" * (4 * 1024 * 1024 + 1), request(usage), now_ms=NOW
        )


@pytest.fixture
def node(tmp_path, entry, monkeypatch):
    path = tmp_path / "panel.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE inbounds (id)")
        db.execute("INSERT INTO inbounds VALUES (7)")
        db.execute("CREATE TABLE client_traffics (id,email,up,down,reset_count)")
        db.executemany(
            "INSERT INTO client_traffics VALUES (?,?,?,?,?)",
            [
                (91, "managed-0@example.test", 100, 200, 2),
                (92, "managed-1@example.test", 300, 400, 3),
                (93, "unrelated", 999, 999, 0),
            ],
        )
    clients = [
        {
            "id": index + 101,
            "uuid": uid,
            "email": f"managed-{index}@example.test",
            "enable": True,
            "inboundIds": [7],
        }
        for index, uid in enumerate(UIDS)
    ]
    state = {"database": path, "events": [], "clients": clients, "drift": False}
    state["inbounds"] = [
        {
            "id": 7,
            "protocol": "vless",
            "settings": {
                "clients": [
                    {"id": row["uuid"], "email": row["email"], "enable": True}
                    for row in clients
                ]
            },
        }
    ]

    class Panel:
        def request(self, method, route):
            assert method == "GET"
            state["events"].append((method, route))
            obj = {
                "panel/api/server/status": {
                    "panelVersion": "3.8.5",
                    "xray": {"state": "running", "errorMsg": ""},
                },
                "panel/api/inbounds/list": state["inbounds"],
                "panel/api/clients/list": state["clients"],
            }[route]
            return {"success": True, "obj": obj}

    state["panel"] = Panel()
    counter = 0

    def identity(*_args, **_kwargs):
        nonlocal counter
        counter += 1
        return ("b" * 64, 123 + (state["drift"] and counter > 1))

    monkeypatch.setattr(entry, "_runtime_identity", identity)
    # Windows has no Unix owners; production validation is separately exercised.
    monkeypatch.setattr(entry, "_validate_private_regular_file", lambda path: None)
    return state


def observe(entry, usage, node):
    return entry.observe_node_usage(
        node["panel"],
        request=request(usage),
        database_path=node["database"],
        now_ms=lambda: NOW + 10,
    )


def test_observer_reads_only_managed_counters_without_mutations(
    entry, usage, node, monkeypatch
):
    original = sqlite3.connect
    sql = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(sql.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    before = node["database"].read_bytes()
    result = observe(entry, usage, node)
    assert [
        (
            key.access_key_id,
            key.record_id,
            key.upload_bytes,
            key.download_bytes,
            key.reset_count,
        )
        for key in result.keys
    ] == [(1, 91, 100, 200, 2), (2, 92, 300, 400, 3)]
    assert all(
        key.source_state == "unsupported" and key.sources == () for key in result.keys
    )
    assert node["database"].read_bytes() == before
    assert not any(
        statement.lstrip()
        .upper()
        .startswith(("UPDATE", "INSERT", "DELETE", "REPLACE", "CREATE", "DROP"))
        for statement in sql
    )
    assert all(method == "GET" for method, _ in node["events"])


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM client_traffics WHERE id=91",
        "UPDATE client_traffics SET up=-1 WHERE id=91",
        "UPDATE client_traffics SET down='oops' WHERE id=91",
        "UPDATE client_traffics SET reset_count=NULL WHERE id=91",
        "UPDATE client_traffics SET email='MANAGED-0@example.test' WHERE id=91",
        "INSERT INTO client_traffics SELECT * FROM client_traffics WHERE id=91",
    ],
)
def test_invalid_or_missing_counter_never_becomes_zero(entry, usage, node, statement):
    with sqlite3.connect(node["database"]) as db:
        db.execute(statement)
    result = observe(entry, usage, node)
    assert result.keys[0].traffic_state == "unavailable"
    assert result.keys[0].upload_bytes is result.keys[0].download_bytes is None
    assert result.keys[1].upload_bytes == 300


@pytest.mark.parametrize("kind", ["email_alias", "uuid_alias", "inbound", "missing"])
def test_ambiguous_or_mismatched_inventory_does_not_attribute(entry, usage, node, kind):
    if kind == "email_alias":
        node["clients"].append(
            dict(node["clients"][0], id=999, email="MANAGED-0@example.test")
        )
    elif kind == "uuid_alias":
        node["clients"].append(
            dict(
                node["clients"][0],
                id=999,
                uuid="11111111-2222-ffff-8444-555555555555",
                email="another",
            )
        )
    elif kind == "inbound":
        node["clients"][0]["inboundIds"] = [8]
    else:
        node["clients"].pop(0)
    result = observe(entry, usage, node)
    assert result.keys[0].traffic_state == "unavailable"


def test_wal_snapshot_reads_committed_pages_and_keeps_files(entry, usage, node):
    writer = sqlite3.connect(node["database"])
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE client_traffics SET up=777 WHERE id=91")
        writer.commit()
        files = {
            path: path.read_bytes()
            for path in node["database"].parent.iterdir()
            if not str(path).endswith("-shm")
        }
        assert observe(entry, usage, node).keys[0].upload_bytes == 777
        assert all(path.read_bytes() == raw for path, raw in files.items())
    finally:
        writer.close()


def test_process_drift_invalidates_entire_batch(entry, usage, node):
    node["drift"] = True
    with pytest.raises(usage.VpnNodeUsageError):
        observe(entry, usage, node)


def test_snapshot_rejects_symlink(entry, usage, node, tmp_path):
    target = tmp_path / "alias.db"
    try:
        target.symlink_to(node["database"])
    except OSError:
        pytest.skip("symlinks unavailable to this Windows token")
    node["database"] = target
    with pytest.raises(usage.VpnNodeUsageError):
        observe(entry, usage, node)


@pytest.mark.parametrize("unsafe", ["owner", "mode", "symlink", "reparse"])
def test_database_metadata_boundary_fails_closed(
    entry, usage, node, monkeypatch, unsafe
):
    from app.services.vpn_node_entrypoint import _validate_private_regular_file

    real_lstat = os.lstat

    def metadata(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        values = {
            name: getattr(info, name) for name in dir(info) if name.startswith("st_")
        }
        values.update(
            st_uid=0,
            st_mode=(stat.S_IFREG | 0o600)
            if Path(path) == node["database"]
            else stat.S_IFDIR | 0o755,
        )
        if Path(path) == node["database"]:
            if unsafe == "owner":
                values["st_uid"] = 99
            elif unsafe == "mode":
                values["st_mode"] |= 0o004
            elif unsafe == "symlink":
                values["st_mode"] = stat.S_IFLNK | 0o777
            else:
                values["st_file_attributes"] = 1024
        return SimpleNamespace(**values)

    monkeypatch.setattr(os, "lstat", metadata)
    monkeypatch.setattr(
        entry, "_validate_private_regular_file", _validate_private_regular_file
    )
    with pytest.raises(usage.VpnNodeUsageError):
        observe(entry, usage, node)
    assert node["events"] == []


def test_observation_duration_overflow_invalidates_batch(
    entry, usage, node, monkeypatch
):
    monotonic = time.monotonic
    state = {"late": False}
    original = node["panel"].request

    def request_after_deadline(*args):
        value = original(*args)
        state["late"] = True
        return value

    monkeypatch.setattr(node["panel"], "request", request_after_deadline)
    monkeypatch.setattr(
        time, "monotonic", lambda: monotonic() + (16 if state["late"] else 0)
    )
    with pytest.raises(usage.VpnNodeUsageError):
        observe(entry, usage, node)


@pytest.fixture
def process(entry, monkeypatch, tmp_path):
    runtime = entry.runtime
    proc, executable, config = (
        tmp_path / "proc",
        tmp_path / "xray",
        tmp_path / "config.json",
    )
    pid = proc / "123"
    value = {
        "api": {"tag": "api", "services": ["HandlerService"]},
        "inbounds": [{"tag": "api", "listen": "127.0.0.1", "port": 62789}],
    }
    data = {
        config: encoded(value),
        pid / "stat": b"123 (xray) S " + b"0 " * 18 + b"456 0\n",
        pid / "cmdline": f"{executable}\0run\0-config\0{config}\0".encode(),
        pid
        / "net/tcp": b"header\n 0: 0100007F:F545 00000000:0000 0A 0:0 0:0 0 0 0 789\n",
        proc / "sys/kernel/random/boot_id": b"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n",
    }
    links = {pid / "exe": str(executable), pid / "fd/3": "socket:[789]"}
    calls = []
    monkeypatch.setattr(entry, "_EXECUTABLE", executable)
    monkeypatch.setattr(entry, "_CONFIG", config)
    monkeypatch.setattr(runtime, "_PROC", proc)
    monkeypatch.setattr(runtime, "_require_linux", lambda: None)
    monkeypatch.setattr(runtime, "_trusted_file", lambda path, deadline: (str(path), 1))
    monkeypatch.setattr(runtime, "_read", lambda path, deadline: data[path])
    monkeypatch.setattr(runtime, "_readlink", lambda path, deadline: links[path])
    monkeypatch.setattr(
        runtime, "_linked_identity", lambda path, deadline: (str(executable), 1)
    )
    monkeypatch.setattr(
        runtime,
        "_entries",
        lambda path, deadline: [pid] if path == proc else [pid / "fd/3"],
    )
    monkeypatch.setattr(
        runtime, "_run", lambda argv, deadline: calls.append(argv) or b"Xray 26.9.9\n"
    )
    return {
        "pid": pid,
        "config": config,
        "value": value,
        "data": data,
        "links": links,
        "calls": calls,
        "executable": executable,
    }


def test_runtime_identity_pins_boot_process_config_namespace_and_owned_loopback(
    entry, process
):
    identity = entry._runtime_identity(time.monotonic() + 5)
    expected = hashlib.sha256(
        b"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\x00123\x00456"
    ).hexdigest()
    assert identity[0] == expected
    assert process["calls"] == [[str(process["executable"]), "version"]]
    old = identity
    process["data"][process["pid"] / "stat"] = (
        b"123 (xray) S " + b"0 " * 18 + b"457 0\n"
    )
    assert entry._runtime_identity(time.monotonic() + 5) != old


@pytest.mark.parametrize(
    "unsafe",
    [
        "public_api",
        "missing_handler",
        "wrong_config",
        "wrong_binary",
        "unowned_listener",
        "invalid_boot",
    ],
)
def test_runtime_identity_rejects_unsafe_runtime(
    entry, usage, process, monkeypatch, unsafe
):
    if unsafe == "public_api":
        process["value"]["inbounds"][0]["listen"] = "0.0.0.0"
    elif unsafe == "missing_handler":
        process["value"]["api"]["services"] = []
    elif unsafe == "wrong_config":
        process["data"][process["pid"] / "cmdline"] = (
            b"xray\0run\0-config\0other.json\0"
        )
    elif unsafe == "wrong_binary":
        monkeypatch.setattr(entry.runtime, "_linked_identity", lambda *_: ("wrong", 1))
    elif unsafe == "unowned_listener":
        process["links"][process["pid"] / "fd/3"] = "socket:[888]"
    else:
        process["data"][entry.runtime._PROC / "sys/kernel/random/boot_id"] = b"invalid"
    process["data"][process["config"]] = encoded(process["value"])
    with pytest.raises(
        (usage.VpnNodeUsageError, entry.runtime.XrayRuntimeError, KeyError)
    ):
        entry._runtime_identity(time.monotonic() + 5)


def test_runtime_config_or_boot_drift_changes_identity(entry, process):
    first = entry._runtime_identity(time.monotonic() + 5)
    process["value"]["extra"] = "changed"
    process["data"][process["config"]] = encoded(process["value"])
    assert entry._runtime_identity(time.monotonic() + 5) != first
    process["data"][entry.runtime._PROC / "sys/kernel/random/boot_id"] = (
        b"bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    )
    assert entry._runtime_identity(time.monotonic() + 5)[0] != first[0]


def test_root_entrypoint_contains_errors_without_output(entry, usage, monkeypatch):
    for raw in (b"bad", usage.serialize_node_usage_request(request(usage), now_ms=NOW)):
        monkeypatch.setattr(
            entry,
            "parse_node_usage_request",
            lambda value: usage.parse_node_usage_request(value, now_ms=NOW),
        )
        out, err = io.BytesIO(), io.BytesIO()
        assert entry.run_node_usage_entrypoint(
            io.BytesIO(raw), out, err, effective_uid=lambda: 1
        ) in (1, 2)
        assert out.getvalue() == err.getvalue() == b""


def test_entrypoint_uses_private_token_get_session_without_journal(
    entry, usage, monkeypatch, tmp_path
):
    req, result = request(usage), receipt(usage)
    config, token, database = (
        tmp_path / "node.json",
        tmp_path / "token",
        tmp_path / "panel.db",
    )
    reads, sessions, observed = [], [], []
    monkeypatch.setattr(
        entry,
        "parse_node_usage_request",
        lambda value: usage.parse_node_usage_request(value, now_ms=NOW),
    )
    monkeypatch.setattr(
        entry,
        "serialize_node_usage_receipt",
        lambda value, req: usage.serialize_node_usage_receipt(value, req, now_ms=NOW),
    )
    monkeypatch.setattr(
        entry, "_node_config", lambda raw: ("http://127.0.0.1/", database)
    )
    monkeypatch.setattr(
        entry,
        "_validate_private_regular_file",
        lambda path: reads.append(("validate", path)),
    )

    def private_file(path, *, limit):
        reads.append(("read", path, limit))
        return b"private-token\n" if path == token else b"{}"

    monkeypatch.setattr(entry, "_read_private_file", private_file)

    class Session:
        def __init__(self, *args, **kwargs):
            sessions.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(entry, "NodePanelSession", Session)

    def observer(panel, **kwargs):
        observed.append(kwargs)
        return result

    out, err = io.BytesIO(), io.BytesIO()
    assert (
        entry.run_node_usage_entrypoint(
            io.BytesIO(usage.serialize_node_usage_request(req, now_ms=NOW)),
            out,
            err,
            config_path=config,
            token_path=token,
            effective_uid=lambda: 0,
            observer=observer,
        )
        == 0
    )
    assert usage.parse_node_usage_receipt(out.getvalue(), req, now_ms=NOW) == result
    assert sessions == [
        (("http://127.0.0.1/",), {"api_token": "private-token", "timeout_seconds": 3})
    ]
    assert observed == [{"request": req, "database_path": database}]
    assert err.getvalue() == b""
    assert {item[1] for item in reads} == {config, token, database}


def test_usage_receipt_strict_nested_shapes_and_counts(usage):
    req, result = request(usage), receipt(usage)
    value = asdict(result)
    cases = []
    for keys in (
        (result.keys[0], result.keys[0]),
        (replace(result.keys[0], access_key_id=99), result.keys[1]),
    ):
        cases.append(encoded(asdict(replace(result, keys=keys))))
    cases.extend(
        [
            encoded(value | {"extra": "private@example.test"}),
            encoded(value).replace(b'"sources":[]', b'"sources":[],"sources":[]', 1),
            encoded(value).replace(b'"sources":[]', b'"sources":{}', 1),
        ]
    )
    for raw in cases:
        with pytest.raises(usage.VpnNodeUsageError):
            usage.parse_node_usage_receipt(raw, req, now_ms=NOW)


def test_maximum_managed_batch_and_receipt_bounds(usage):
    identities = tuple(
        usage.UsageIdentity(
            index, 7, f"{index:08x}-2222-4333-8444-555555555555", f"managed-{index}"
        )
        for index in range(1, 2049)
    )
    req = replace(request(usage), identities=identities)
    assert (
        usage.parse_node_usage_request(
            usage.serialize_node_usage_request(req, now_ms=NOW), now_ms=NOW
        )
        == req
    )
    sources = tuple(f"2001:db8:abcd:abcd:abcd:abcd:abcd:{i:04x}" for i in range(64))
    result = replace(
        receipt(usage),
        keys=tuple(
            usage.KeyUsage(index, "ok", index, 0, 0, 0, "ok", sources)
            for index in range(1, 2049)
        ),
    )
    with pytest.raises(usage.VpnNodeUsageError):
        usage.serialize_node_usage_receipt(result, req, now_ms=NOW)


def test_isolated_bundle_routes_usage_and_probes_imports(tmp_path):
    from app.services.vpn_node_bundle import BUNDLE_MEMBERS, build_node_bundle

    assert "app/services/vpn_node_usage.py" in BUNDLE_MEMBERS
    assert "app/services/vpn_node_usage_entrypoint.py" in BUNDLE_MEMBERS
    bundle = tmp_path / "node.pyz"
    build_node_bundle(Path(__file__).resolve().parents[1], bundle)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(bundle), "--usage"],
        input=b"bad",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == result.stderr == b""


def test_isolated_bundle_observes_fake_node_without_site_packages(tmp_path, node):
    from app.services.vpn_node_bundle import build_node_bundle

    bundle = tmp_path / "node.pyz"
    build_node_bundle(Path(__file__).resolve().parents[1], bundle)
    script = f"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, {str(bundle)!r})
from app.services import vpn_node_usage as usage, vpn_node_usage_entrypoint as entry
now = time.time_ns() // 1_000_000
minute = now // 60_000 * 60_000
# A fixed injected clock keeps this protocol test independent of wall-clock minute position.
entry._runtime_identity = lambda deadline: ('a' * 64, 123)
entry._validate_private_regular_file = lambda path: None
inbounds = {node["inbounds"]!r}
clients = {node["clients"]!r}
class Panel:
    def request(self, method, route):
        assert method == 'GET'
        obj = {{'panel/api/inbounds/list': inbounds, 'panel/api/clients/list': clients,
               'panel/api/server/status': {{'panelVersion': '3.8.5', 'xray': {{'state': 'running', 'errorMsg': ''}}}}}}[route]
        return {{'success': True, 'obj': obj}}
request = usage.UsageRequest(1, {POLL!r}, minute, tuple(
    usage.UsageIdentity(i + 1, 7, uid, 'managed-' + str(i) + '@example.test')
    for i, uid in enumerate({UIDS!r})))
result = entry.observe_node_usage(Panel(), request=request, database_path=Path({str(node["database"])!r}), now_ms=lambda: minute + 10)
assert [(key.upload_bytes, key.download_bytes) for key in result.keys] == [(100, 200), (300, 400)]
assert all(key.source_state == 'unsupported' for key in result.keys)
print('usage-observation-ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b"usage-observation-ok"
    assert result.stderr == b""


def test_existing_bundle_import_gate_rejects_broken_usage_before_replacement(tmp_path):
    from app.services.vpn_node_bundle import (
        BUNDLE_MEMBERS,
        NodeBundleError,
        build_node_bundle,
    )

    backend = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    for member in BUNDLE_MEMBERS:
        target = source / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((backend / member).read_bytes())
    (source / "app/services/vpn_node_usage.py").write_text(
        "import nonexistent_usage_dependency\n", encoding="utf-8"
    )
    bundle = tmp_path / "node.pyz"
    bundle.write_bytes(b"previous")
    with pytest.raises(NodeBundleError):
        build_node_bundle(source, bundle)
    assert bundle.read_bytes() == b"previous"


@pytest.mark.asyncio
async def test_usage_transport_fixed_command_larger_bound_and_safe_errors(usage):
    transport = importlib.import_module("app.services.vpn_node_transport")

    commands, inputs = [], []
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    snapshot = transport.VpnNodeTransportSnapshot(
        "host",
        22,
        "root",
        b"host " + host_key.export_public_key("openssh"),
        password="private",
    )
    raw = usage.serialize_node_usage_receipt(receipt(usage), request(usage), now_ms=NOW)

    class Stdin:
        def write(self, value):
            inputs.append(value)

        async def drain(self):
            pass

        def write_eof(self):
            pass

    class Reader:
        def __init__(self, value):
            self.value = value

        async def read(self, count):
            value, self.value = self.value[:count], self.value[count:]
            return value

    class Process:
        stdin = Stdin()
        exit_status = 0

        def __init__(self, output):
            self.stdout, self.stderr = Reader(output), Reader(b"")

        async def wait(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    state = {"raw": raw, "aborted": False}

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def create_process(self, command, **kwargs):
            commands.append((command, kwargs))
            return Process(state["raw"])

        def abort(self):
            state["aborted"] = True

        async def wait_closed(self):
            pass

    async def connector(*args, **kwargs):
        assert kwargs["config"] is kwargs["agent_path"] is None
        assert kwargs["known_hosts"]
        return Connection()

    assert await transport.execute_vpn_node_usage_over_ssh(
        snapshot, request(usage), now_ms=NOW, connector=connector
    ) == receipt(usage)
    assert commands == [
        (
            transport.FIXED_NODE_COMMAND + " --usage",
            {"term_type": None, "encoding": None},
        )
    ]
    assert inputs == [usage.serialize_node_usage_request(request(usage), now_ms=NOW)]
    for raw in (b"private@example.test 203.0.113.1", b"x" * (4 * 1024 * 1024 + 1)):
        state["raw"] = raw
        with pytest.raises(transport.VpnNodeTransportError) as caught:
            await transport.execute_vpn_node_usage_over_ssh(
                snapshot, request(usage), now_ms=NOW, connector=connector
            )
        assert caught.value.__context__ is None
        assert "203.0.113.1" not in "".join(traceback.format_exception(caught.value))
        assert state["aborted"]


@pytest.mark.asyncio
async def test_usage_transport_pin_and_output_larger_than_health_limit(usage):
    transport = importlib.import_module("app.services.vpn_node_transport")

    identities = tuple(
        usage.UsageIdentity(
            index, 7, f"{index:08x}-2222-4333-8444-555555555555", f"managed-{index}"
        )
        for index in range(1, 81)
    )
    req = replace(request(usage), identities=identities)
    sources = tuple(f"2001:db8::{index}" for index in range(64))
    result = replace(
        receipt(usage),
        keys=tuple(
            usage.KeyUsage(index, "ok", index, 0, 0, 0, "ok", sources)
            for index in range(1, 81)
        ),
    )
    raw = usage.serialize_node_usage_receipt(result, req, now_ms=NOW)
    assert 64 * 1024 < len(raw) < usage.MAX_USAGE_RECEIPT_BYTES
    calls = []
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "root" and password == "private"

    async def process_factory(process):
        calls.append((process.command, process.term_type, await process.stdin.read()))
        process.stdout.write(raw)
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=Server,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = transport.VpnNodeTransportSnapshot(
            "127.0.0.1",
            port,
            "root",
            f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key("openssh"),
            password="private",
        )
        assert (
            await transport.execute_vpn_node_usage_over_ssh(snapshot, req, now_ms=NOW)
            == result
        )
        assert calls == [
            (
                transport.FIXED_NODE_USAGE_COMMAND,
                None,
                usage.serialize_node_usage_request(req, now_ms=NOW),
            )
        ]
        forged = replace(
            snapshot,
            known_hosts=f"[127.0.0.1]:{port} ".encode()
            + asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh"),
        )
        with pytest.raises(transport.VpnNodeTransportError):
            await transport.execute_vpn_node_usage_over_ssh(forged, req, now_ms=NOW)
        assert len(calls) == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_usage_timeout_and_cancellation_clean_up_process(
    usage, monkeypatch, cancel
):
    transport = importlib.import_module("app.services.vpn_node_transport")

    monkeypatch.setattr(
        transport, "REMOTE_OPERATION_TIMEOUT", 0.02 if not cancel else 10
    )
    events, entered = [], asyncio.Event()

    class Stdin:
        def write(self, value):
            pass

        async def drain(self):
            entered.set()

        def write_eof(self):
            pass

    class Reader:
        async def read(self, count):
            try:
                await asyncio.Event().wait()
            finally:
                events.append("reader_stopped")

    class Process:
        stdin, stdout, stderr = Stdin(), Reader(), Reader()

        def close(self):
            events.append("process_closed")

        async def wait_closed(self):
            pass

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def create_process(self, *args, **kwargs):
            return Process()

        def abort(self):
            events.append("aborted")

        async def wait_closed(self):
            pass

    async def connector(*args, **kwargs):
        return Connection()

    key = asyncssh.generate_private_key("ssh-ed25519")
    snapshot = transport.VpnNodeTransportSnapshot(
        "host",
        22,
        "root",
        b"host " + key.export_public_key("openssh"),
        password="private",
    )
    task = asyncio.create_task(
        transport.execute_vpn_node_usage_over_ssh(
            snapshot, request(usage), now_ms=NOW, connector=connector
        )
    )
    await entered.wait()
    if cancel:
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(
        asyncio.CancelledError if cancel else transport.VpnNodeTransportError
    ):
        await task
    assert events.count("process_closed") == events.count("aborted") == 1
    assert events.count("reader_stopped") == 2
