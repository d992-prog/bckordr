from __future__ import annotations

import base64
import importlib
import json
import sqlite3
import subprocess
import sys
import threading
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
from uuid import UUID, uuid4

import pytest

from app.services.vpn_endpoint_types import VpnEndpointTarget
from app.services.vpn_node_journal import initialize_node_journal
from app.services.vpn_node_request import (
    VpnNodeRequest,
    VpnNodeRequestError,
    node_request_digest,
)
from app.services.vpn_xray_runtime import XrayRuntimeError, XrayRuntimeObservation
from app.services.vpn_xui_node_http import NodePanelSession


SECRET = "synthetic-secret-kept-on-node"
UID = UUID("11111111-2222-4333-8444-555555555555")
OTHER = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def record(uid=UID, email="private-email", record_id=19):
    return {
        "id": record_id,
        "uuid": str(uid),
        "email": email,
        "subId": "persisted-sub-id",
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
        "createdAt": 0,
        "updatedAt": 0,
        "inboundIds": [3],
    }


def test_executor_module_exists():
    assert importlib.util.find_spec("app.services.vpn_xui_node_executor") is not None


@pytest.fixture
def executor():
    assert importlib.util.find_spec("app.services.vpn_xui_node_executor") is not None
    return importlib.import_module("app.services.vpn_xui_node_executor")


@pytest.fixture
def node(tmp_path):
    directory = tmp_path / "journal"
    directory.mkdir(mode=0o700)
    initialize_node_journal(directory)
    database = tmp_path / "panel.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.executemany("INSERT INTO inbounds VALUES (?)", [(3,), (7,)])
        connection.execute(
            "CREATE TABLE client_traffics (id,email,up,down,reset_count)"
        )
        connection.execute(
            "INSERT INTO client_traffics VALUES (90,'private-email',10,20,2)"
        )
    request = VpnNodeRequest(
        uuid4(),
        11,
        1,
        "provision",
        VpnEndpointTarget(
            1,
            2,
            3,
            "vpn.example.test",
            443,
            "vless",
            "tcp",
            "reality",
            "example.test",
            KEY,
            "abcd",
            "chrome",
            "xtls-rprx-vision",
        ),
        UID,
        "private-email",
        "persisted-sub-id",
        0,
        1000,
        1234,
        False,
        True,
    )
    data = dict(
        directory=directory,
        database=database,
        request=request,
        records=[
            record(),
            dict(record(OTHER, "other", 20), inboundIds=[7], subId="other-sub-id"),
        ],
        events=[],
        writes=[],
        mark_checks=[],
        process=100,
        auto_restart=True,
        runtime_fault=None,
        fault=None,
        preflight=None,
        status_version="3.8.5",
        runtime_reads=0,
        get_override=None,
        inbounds_override=None,
        auth_failure=False,
    )

    def inbounds():
        rows = []
        for number, port in ((3, 443), (7, 8443)):
            clients = [
                dict(id=r["uuid"], email=r["email"], enable=r["enable"], flow=r["flow"])
                for r in data["records"]
                if number in (r["inboundIds"] or [])
            ]
            rows.append(
                dict(
                    id=number,
                    protocol="vless",
                    enable=True,
                    port=port,
                    trafficReset="never",
                    settings=dict(decryption="none", clients=clients),
                    streamSettings=dict(
                        network="raw",
                        security="reality",
                        realitySettings=dict(
                            serverNames=["example.test"],
                            shortIds=["abcd"],
                            privateKey=SECRET,
                            settings=dict(
                                publicKey=KEY, fingerprint="chrome", serverName=""
                            ),
                        ),
                    ),
                )
            )
        if data["inbounds_override"]:
            data["inbounds_override"](rows)
        return rows

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            data["events"].append(("GET", self.path))
            if data["auth_failure"]:
                self.respond({"success": False, "msg": SECRET})
                return
            if self.path.endswith("/status"):
                obj = dict(
                    panelVersion=data["status_version"],
                    xray=dict(state="running", errorMsg=""),
                )
                if (
                    data["fault"] == "status_stopping"
                    and data["writes"]
                    and data["runtime_reads"] < 4
                ):
                    obj["xray"]["state"] = "stop"
            elif self.path.endswith("/inbounds/list"):
                obj = inbounds()
            elif self.path.endswith("/clients/list"):
                obj = deepcopy(data["records"])
            else:
                email = unquote(self.path.rsplit("/", 1)[1])
                row = next(r for r in data["records"] if r["email"] == email)
                obj = dict(
                    client={
                        k: v
                        for k, v in row.items()
                        if k not in ("inboundIds", "traffic")
                    },
                    inboundIds=row["inboundIds"],
                    externalLinks=[],
                    usedTraffic=0,
                    tunnelAllowedIPs={},
                )
                if data["get_override"]:
                    data["get_override"](obj)
            self.respond(dict(success=True, obj=obj))

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data["events"].append(("POST", self.path))
            data["writes"].append((self.path, body))
            with sqlite3.connect(directory / "operations.sqlite3") as connection:
                data["mark_checks"].append(
                    connection.execute(
                        "SELECT phase FROM operations WHERE operation_id=?",
                        (str(data["request"].operation_id),),
                    ).fetchone()
                    == ("mutating",)
                )
            if self.path.endswith("/restartXrayService"):
                if data["runtime_fault"] != "old_process":
                    data["process"] += 1
            elif self.path.endswith("/add"):
                wire = body["client"]
                added = {
                    k: v
                    for k, v in wire.items()
                    if k not in ("id", "created_at", "updated_at")
                }
                added.update(
                    id=21,
                    uuid=wire["id"],
                    createdAt=wire["created_at"],
                    updatedAt=wire["updated_at"],
                    allowedIPs=",".join(wire["allowedIPs"]),
                    inboundIds=body["inboundIds"],
                )
                data["records"].append(added)
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "INSERT INTO client_traffics VALUES (91,?,0,0,0)",
                        (wire["email"],),
                    )
            elif "/update/" in self.path:
                row = next(r for r in data["records"] if r["email"] == body["email"])
                changed = {
                    k: v
                    for k, v in body.items()
                    if k not in ("id", "created_at", "updated_at")
                }
                changed.update(
                    uuid=body["id"],
                    createdAt=body["created_at"],
                    updatedAt=body["updated_at"],
                    allowedIPs=",".join(body["allowedIPs"]),
                )
                row.update(changed)
            else:
                row = next(
                    r for r in data["records"] if r["email"] == body["emails"][0]
                )
                row["enable"] = self.path.endswith("/bulkEnable")
                if not row["enable"] and data["auto_restart"]:
                    data["process"] += 1
            fault = data["fault"]
            if fault in ("counter_reset", "counter_id", "counter_drop", "counter_down"):
                sql = {
                    "counter_reset": "UPDATE client_traffics SET reset_count=3",
                    "counter_id": "UPDATE client_traffics SET id=92",
                    "counter_drop": "DELETE FROM client_traffics",
                    "counter_down": "UPDATE client_traffics SET up=0",
                }[fault]
                with sqlite3.connect(database) as connection:
                    connection.execute(sql)
            if fault == "unrelated_drift":
                data["records"][1]["limitIp"] += 1
            if fault == "target_drift":
                data["records"][0]["password"] = "drift"
            if fault == "lost_reply":
                self.close_connection = True
                return
            if fault == "malformed":
                self.respond([SECRET])
                return
            obj = dict(changed=1) if "/bulk" in self.path else None
            if fault == "bool_changed":
                obj = dict(changed=True)
            if fault == "skipped":
                obj = dict(changed=1, skipped=["private-email"])
            if fault == "nested_pending":
                obj = dict(nodePending=True)
            self.respond(
                dict(success=fault != "false", obj=obj, nodePending=fault == "pending")
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs=dict(poll_interval=0.01)
    )
    thread.start()
    data["factory"] = lambda: NodePanelSession(
        f"http://127.0.0.1:{server.server_port}/", api_token=SECRET
    )

    def runtime_reader(**kwargs):
        data["runtime_reads"] += 1
        assert kwargs["port"] == 443 and kwargs["client_uuid"] == UID
        assert (
            kwargs["client_email"] == "private-email"
            and kwargs["flow"] == "xtls-rprx-vision"
        )
        fault = data["runtime_fault"]
        if fault == "transient" and data["writes"] and data["runtime_reads"] < 4:
            raise XrayRuntimeError("vpn_xray_runtime_unavailable")
        if fault == "new_still_present" and data["writes"]:
            return XrayRuntimeObservation("matched", data["process"] + 1, 123)
        if fault == "post_unavailable" and data["writes"]:
            raise XrayRuntimeError("vpn_xray_runtime_unavailable")
        if fault == "conflict":
            raise XrayRuntimeError("vpn_xray_runtime_conflict")
        if fault == "unavailable":
            raise XrayRuntimeError("vpn_xray_runtime_unavailable")
        row = next((r for r in data["records"] if r["uuid"] == str(UID)), None)
        present = row is not None and row["enable"]
        if fault == "absent" or (fault == "not_applied" and data["writes"]):
            present = False
        return XrayRuntimeObservation(
            "matched" if present else "not_observed", data["process"], 123
        )

    data["reader"] = runtime_reader
    try:
        yield data
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run(executor, node, **changes):
    if changes:
        node["request"] = replace(node["request"], **changes)
    return executor.execute_node_client_operation(
        node["request"],
        journal_directory=node["directory"],
        database_path=node["database"],
        panel_factory=node["factory"],
        runtime_reader=node["reader"],
    )


def next_run(executor, node, **changes):
    return run(
        executor,
        node,
        operation_id=uuid4(),
        generation=node["request"].generation + 1,
        **changes,
    )


def test_create_suspend_resume_revoke_preserves_identity_and_other_policy(
    executor, node, capsys, caplog
):
    node["records"].pop(0)
    other = deepcopy(node["records"])
    with sqlite3.connect(node["database"]) as connection:
        connection.execute("DELETE FROM client_traffics")
    assert run(executor, node, allow_create=True).state == "observed"
    created = deepcopy(node["records"][-1])
    assert created["uuid"] == str(UID) and created["subId"] == "persisted-sub-id"
    assert (
        created["createdAt"] == 1234
        and created["limitIp"] == 0
        and created["password"] == ""
    )
    assert type(created["tgId"]) is int and created["tgId"] == 0
    assert (
        next_run(executor, node, action="suspend", allow_create=False).state
        == "observed"
    )
    assert next_run(executor, node, action="provision").state == "observed"
    assert next_run(executor, node, action="revoke").state == "observed"
    assert node["records"][:-1] == other
    assert node["records"][-1] == dict(created, enable=False)
    writes = len(node["writes"])
    assert run(executor, node).state == "observed"
    assert (
        next_run(executor, node, action="provision").error_code
        == "vpn_node_key_revoked"
    )
    assert len(node["writes"]) == writes and all(node["mark_checks"])
    raw = b"".join(p.read_bytes() for p in node["directory"].iterdir())
    for secret in (SECRET, str(UID), "private-email", "persisted-sub-id", KEY):
        assert secret.encode() not in raw
    assert capsys.readouterr() == ("", "") and caplog.text == ""


def test_3x_ui_default_spider_path_allows_new_client(executor, node):
    def add_default_spider(rows):
        rows[0]["streamSettings"]["realitySettings"]["settings"]["spiderX"] = "/"

    node["inbounds_override"] = add_default_spider
    node["records"].pop(0)
    with sqlite3.connect(node["database"]) as connection:
        connection.execute("DELETE FROM client_traffics")

    assert run(executor, node, allow_create=True).state == "observed"


def test_readonly_noop_and_digest_owned_by_executor(executor, node):
    before = node["database"].read_bytes()
    assert run(executor, node).state == "observed"
    assert node["writes"] == [] and node["database"].read_bytes() == before
    with sqlite3.connect(node["directory"] / "operations.sqlite3") as connection:
        assert connection.execute(
            "SELECT request_digest FROM operations"
        ).fetchone() == (node_request_digest(node["request"]),)
    events = list(node["events"])
    assert run(executor, node).state == "observed"
    assert run(executor, node, operation_id=uuid4()).state == "stale"
    assert node["events"] == events


def test_full_update_is_complete_and_preserves_all_other_fields(executor, node):
    before = deepcopy(node["records"])
    assert run(executor, node, traffic_limit_bytes=2000).state == "observed"
    assert node["records"] == [dict(before[0], totalGB=2000), before[1]]
    path, wire = node["writes"][0]
    assert path == "/panel/api/clients/update/private-email?inboundIds=3"
    expected = {
        k: v
        for k, v in before[0].items()
        if k not in ("id", "uuid", "createdAt", "updatedAt", "inboundIds")
    }
    expected.update(
        id=str(UID), created_at=0, updated_at=0, allowedIPs=[], totalGB=2000
    )
    assert wire == expected


def test_unrelated_inbound_policy_drift_after_write_is_uncertain(executor, node):
    def drift(rows):
        if node["writes"]:
            rows[1].update(port=9999, trafficReset="month", enable=False)

    node["inbounds_override"] = drift
    assert run(executor, node, traffic_limit_bytes=2000).state == "uncertain"
    assert len(node["writes"]) == 1
    assert next_run(executor, node).state == "blocked"


@pytest.mark.parametrize("inbound_index", [0, 1])
@pytest.mark.parametrize(
    "field",
    [
        "total",
        "expiryTime",
        "lastTrafficResetTime",
        "sniffing",
        "settings",
        "streamSettings",
    ],
)
def test_inbound_nonclient_policy_is_preserved(executor, node, inbound_index, field):
    def drift(rows):
        if node["writes"]:
            row = rows[inbound_index]
            if field in ("settings", "streamSettings"):
                row[field]["preserved-option"] = "changed"
            elif field == "sniffing":
                row[field] = {"enabled": True, "destOverride": ["http"]}
            else:
                row[field] = 999

    node["inbounds_override"] = drift
    assert run(executor, node, traffic_limit_bytes=2000).state == "uncertain"
    assert len(node["writes"]) == 1


@pytest.mark.parametrize("same_inbound", [False, True])
def test_other_effective_client_policy_is_preserved(executor, node, same_inbound):
    if same_inbound:
        node["records"][1]["inboundIds"] = [3]

    def drift(rows):
        if node["writes"]:
            for row in rows:
                for client in row["settings"]["clients"]:
                    if client["email"] == "other":
                        client["flow"] = ""

    node["inbounds_override"] = drift
    assert run(executor, node, traffic_limit_bytes=2000).state == "uncertain"
    assert len(node["writes"]) == 1


def test_inbound_live_traffic_may_increase_without_policy_drift(executor, node):
    def traffic(rows):
        up, down = (11, 22) if node["writes"] else (10, 20)
        for row in rows:
            row.update(
                up=up,
                down=down,
                clientStats=[{"email": "other", "up": up, "down": down}],
            )

    node["inbounds_override"] = traffic
    assert run(executor, node, traffic_limit_bytes=2000).state == "observed"
    assert len(node["writes"]) == 1


@pytest.mark.parametrize("tg_id", [0, 123456789, -1001234567890, -(2**63), 2**63 - 1])
def test_canonical_signed_integer_tg_id_is_preserved_in_full_update(
    executor, node, tg_id
):
    node["records"][0]["tgId"] = tg_id
    assert run(executor, node, traffic_limit_bytes=2000).state == "observed"
    assert type(node["records"][0]["tgId"]) is int
    assert node["records"][0]["tgId"] == tg_id
    wire = node["writes"][0][1]
    assert type(wire["tgId"]) is int and wire["tgId"] == tg_id


@pytest.mark.parametrize(
    "tg_id", [True, False, "123456789", "", None, 1.0, -(2**63) - 1, 2**63]
)
def test_malformed_tg_id_is_rejected_before_write(executor, node, tg_id):
    node["records"][0]["tgId"] = tg_id
    assert run(executor, node, traffic_limit_bytes=2000).state == "failed"
    assert node["writes"] == []


@pytest.mark.parametrize("action", ["provision", "suspend", "revoke"])
def test_legacy_empty_sub_id_preserves_password_limit_and_creation(
    executor, node, action
):
    node["records"][0]["subId"] = ""
    node["records"][0]["enable"] = action != "provision"
    before = deepcopy(node["records"][0])
    assert run(executor, node, sub_id="", action=action).state == "observed"
    assert node["records"][0] == dict(before, enable=action == "provision")
    assert all("/update/" not in path for path, _ in node["writes"])


def test_empty_sub_id_cannot_update_or_false_succeed_without_runtime(executor, node):
    node["records"][0]["subId"] = ""
    assert run(executor, node, sub_id="", traffic_limit_bytes=2000).state == "failed"
    node["runtime_fault"] = "absent"
    assert next_run(executor, node, traffic_limit_bytes=1000).state == "failed"
    assert node["writes"] == []


def test_creation_rejects_nonempty_sub_id_used_by_another_client(executor, node):
    node["records"].pop(0)
    node["records"][0]["subId"] = "persisted-sub-id"
    with sqlite3.connect(node["database"]) as connection:
        connection.execute("DELETE FROM client_traffics")
    assert run(executor, node, allow_create=True).state == "failed"
    assert node["writes"] == []


@pytest.mark.parametrize("action", ["provision", "suspend"])
def test_postwrite_transient_runtime_unavailability_may_settle(executor, node, action):
    node["runtime_fault"] = "transient"
    assert (
        run(executor, node, action=action, traffic_limit_bytes=2000).state == "observed"
    )
    assert len(node["writes"]) == 1


@pytest.mark.parametrize("action", ["provision", "suspend"])
def test_postwrite_cached_status_can_settle_without_another_write(
    executor, node, action
):
    node["fault"] = "status_stopping"
    assert (
        run(executor, node, action=action, traffic_limit_bytes=2000).state == "observed"
    )
    assert len(node["writes"]) == 1


@pytest.mark.parametrize("fault", ["new_still_present", "post_unavailable"])
def test_forced_restart_needs_positive_old_generation_observation(
    executor, node, fault
):
    node["runtime_fault"] = fault
    node["auto_restart"] = False
    assert run(executor, node, action="suspend").state == "uncertain"
    assert len(node["writes"]) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "sub_id",
        "missing",
        "counter_orphan",
        "alias_uuid",
        "alias_email",
        "orphan",
        "split",
        "scoped",
        "version",
        "transport",
        "runtime",
        "reset",
        "reset_day",
        "hwid",
        "keepalive",
        "renewal",
        "inbound_renewal",
        "external",
        "tunnel",
        "flow",
        "counter_missing",
        "counter_duplicate",
        "counter_malformed",
        "counter_case",
        "expired",
        "restart_permission",
        "record_missing",
        "record_extra",
        "nonlocal",
    ],
)
def test_preflight_refuses_unsafe_state_without_mutation(executor, node, fault):
    row = node["records"][0]
    changes = {}
    if fault == "sub_id":
        row["subId"] = "different"
    elif fault in ("missing", "counter_orphan"):
        node["records"].pop(0)
        changes["allow_create"] = fault == "counter_orphan"
    elif fault in ("alias_uuid", "alias_email", "orphan"):
        added = record(
            UUID("11111111-2222-4999-8444-555555555555")
            if fault == "alias_uuid"
            else uuid4(),
            "PRIVATE-EMAIL" if fault != "alias_uuid" else "alias",
            21,
        )
        added["inboundIds"] = None if fault == "orphan" else [7]
        added["enable"] = False
        node["records"].append(added)
    elif fault == "split":
        row["inboundIds"] = [3, 7]
    elif fault == "scoped":
        node["inbounds_override"] = lambda rows: rows.pop()
    elif fault == "version":
        node["status_version"] = "3.8.4"
    elif fault == "transport":
        node["inbounds_override"] = lambda rows: rows[0].update(port=444)
    elif fault == "runtime":
        node["runtime_fault"] = "conflict"
    elif fault in ("reset", "reset_day", "hwid", "keepalive"):
        row[
            {
                "reset": "reset",
                "reset_day": "resetDay",
                "hwid": "limitHwid",
                "keepalive": "keepAlive",
            }[fault]
        ] = 1
    elif fault == "renewal":
        row["trafficReset"] = "month"
    elif fault == "inbound_renewal":
        node["inbounds_override"] = lambda rows: rows[0].update(trafficReset="month")
    elif fault == "external":
        node["get_override"] = lambda obj: obj.update(externalLinks=[SECRET])
    elif fault == "tunnel":
        node["get_override"] = lambda obj: obj.update(
            tunnelAllowedIPs={"3": ["10.0.0.0/8"]}
        )
    elif fault == "flow":
        node["get_override"] = lambda obj: obj["client"].update(flow="")
    elif fault.startswith("counter_"):
        sql = {
            "counter_missing": "DELETE FROM client_traffics",
            "counter_duplicate": "INSERT INTO client_traffics SELECT * FROM client_traffics",
            "counter_malformed": "UPDATE client_traffics SET up=-1",
            "counter_case": "UPDATE client_traffics SET email='PRIVATE-EMAIL'",
        }[fault]
        with sqlite3.connect(node["database"]) as connection:
            connection.execute(sql)
    elif fault == "expired":
        changes["expires_at_ms"] = 1
    elif fault == "restart_permission":
        changes.update(action="suspend", allow_shared_restart=False)
    elif fault == "record_missing":
        row.pop("limitHwid")
    elif fault == "record_extra":
        row["futurePolicy"] = SECRET
    elif fault == "nonlocal":
        node["inbounds_override"] = lambda rows: rows[0].update(nodeId=99)
    assert run(executor, node, **changes).state == "failed"
    assert node["writes"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "malformed",
        "false",
        "lost_reply",
        "pending",
        "nested_pending",
        "bool_changed",
        "skipped",
        "counter_reset",
        "counter_id",
        "counter_drop",
        "counter_down",
        "target_drift",
        "unrelated_drift",
    ],
)
def test_any_ambiguous_write_blocks_later_operations(executor, node, fault):
    node["fault"] = fault
    receipt = run(executor, node, action="suspend")
    assert (receipt.state, receipt.error_code) == (
        "uncertain",
        "vpn_node_mutation_uncertain",
    )
    assert len(node["writes"]) == 1 and node["mark_checks"] == [True]
    assert (
        next_run(executor, node, access_key_id=22).error_code
        == "vpn_node_reconciliation_required"
    )
    assert len(node["writes"]) == 1
    assert SECRET not in repr(receipt)


def test_auth_failure_still_records_permanent_revoke(executor, node):
    node["auth_failure"] = True
    assert run(executor, node, action="revoke").state == "failed"
    assert (
        next_run(executor, node, action="provision").error_code
        == "vpn_node_key_revoked"
    )
    assert node["writes"] == []


@pytest.mark.parametrize("enabled", [False, True])
def test_old_process_requires_one_forced_restart(executor, node, enabled):
    node["records"][0]["enable"] = enabled
    node["auto_restart"] = False
    assert run(executor, node, action="suspend").state == "observed"
    assert [path for path, _ in node["writes"]].count(
        "/panel/api/server/restartXrayService"
    ) == 1
    assert all(node["mark_checks"])


@pytest.mark.parametrize("fault", ["old_process", "not_applied"])
def test_runtime_postcondition_failure_is_uncertain(executor, node, fault):
    node["runtime_fault"] = fault
    node["auto_restart"] = False
    action = "suspend" if fault == "old_process" else "provision"
    assert (
        run(executor, node, action=action, traffic_limit_bytes=2000).state
        == "uncertain"
    )
    assert len(node["writes"]) == (2 if fault == "old_process" else 1)
    assert next_run(executor, node).state == "blocked"


def test_invalid_request_rejected_before_journal_or_auth(executor, node, tmp_path):
    with pytest.raises(VpnNodeRequestError):
        executor.execute_node_client_operation(
            replace(node["request"], generation=True),
            journal_directory=tmp_path / "absent",
            database_path=node["database"],
            panel_factory=node["factory"],
            runtime_reader=node["reader"],
        )
    assert node["events"] == []
    assert not (tmp_path / "absent").exists()


def test_executor_import_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import app.services.vpn_xui_node_executor"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
