import ast
import json
import shlex
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.db.models import VpnAccessKey, WorkerNode
from app.services import vpn_provisioning


CLIENT_UUID = "11111111-1111-1111-1111-111111111111"
EMAIL = "dropcatch-12-phone"
NEW_EXPIRY = 1800000000000


def remote_program(operation="provision"):
    worker = WorkerNode(id=7, name="vpn-node", vpn_inbound_id=1, vpn_public_host="vpn.example.test")
    if operation == "provision":
        command = vpn_provisioning.build_vpn_client_provision_command(
            worker,
            vpn_provisioning.VpnClientProvisionPayload(
                client_uuid=UUID(CLIENT_UUID), client_email=EMAIL, inbound_id=1,
                expires_at=datetime.fromtimestamp(NEW_EXPIRY / 1000, tz=UTC),
                traffic_limit_gb=25, max_devices=3,
            ),
        )
    else:
        builder = getattr(vpn_provisioning, "build_vpn_client_suspend_command", None)
        assert callable(builder), "reversible suspension command is missing"
        command = builder(worker, VpnAccessKey(id=12, public_name="phone", external_uuid=CLIENT_UUID))
    shell_script = shlex.split(command)[-1]
    script = shell_script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(script)
    definitions = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    namespace = {}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), "<remote-definitions>", "exec"), namespace)
    exported = next(line for line in shell_script.splitlines() if line.startswith("export "))
    namespace["payload"] = json.loads(shlex.split(exported)[1].split("=", 1)[1])
    # Execute the real remote entry point separately, after replacing only machine I/O.
    start = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "db_path" for target in node.targets))
    main = compile(ast.Module(body=tree.body[start:], type_ignores=[]), "<remote-main>", "exec")
    return namespace, main


@pytest.fixture(params=["legacy", "normalized"])
def panel_db(request):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("create table inbounds (id integer primary key, protocol text, port integer, settings text)")
    clients = [
        {"id": CLIENT_UUID, "email": EMAIL, "enable": True, "flow": "xtls-rprx-vision",
         "subId": "original-subscription", "tgId": "12345", "reset": 17,
         "limitIp": 1, "totalGB": 99, "expiryTime": 100, "custom": "keep"},
        {"id": "unrelated-uuid", "email": "unrelated", "enable": True, "custom": "untouched"},
    ]
    settings = {"clients": clients, "decryption": "none"}
    conn.execute("insert into inbounds values (1, 'vless', 443, ?)", (json.dumps(settings),))
    conn.execute("insert into inbounds values (2, 'vless', 8443, ?)", (json.dumps({"clients": [clients[1]]}),))
    conn.execute("""create table client_traffics (
        id integer primary key, inbound_id integer, email text, client_id integer,
        enable integer, up integer, down integer, last_online integer,
        total integer, expiry_time integer, reset integer)""")
    conn.execute("insert into client_traffics values (10, 1, ?, 5, 1, 123, 456, 789, 99, 100, 17)", (EMAIL,))
    conn.execute("insert into client_traffics values (11, 1, 'unrelated', 6, 1, 987, 654, 321, 77, 100, 9)")
    if request.param == "normalized":
        conn.execute("""create table clients (
            id integer primary key, uuid text, email text, enabled integer,
            flow text, sub_id text, tg_id integer, limit_ip integer,
            total_gb integer, expiry_time integer, reset integer)""")
        conn.execute("insert into clients values (5, ?, ?, 1, 'vision', 'sub-5', 12345, 1, 99, 100, 17)", (CLIENT_UUID, EMAIL))
        conn.execute("insert into clients values (6, 'unrelated-uuid', 'unrelated', 1, 'flow-6', 'sub-6', '', 1, 77, 100, 9)")
        conn.execute("""create table client_inbounds (
            id integer primary key, client_id integer, inbound_id integer,
            flow_override text, created_at text)""")
        conn.execute("insert into client_inbounds values (20, 5, 1, 'vision-override', '2026-01-01')")
        conn.execute("insert into client_inbounds values (21, 6, 1, 'unrelated-override', '2026-02-01')")
    conn.commit()
    yield conn
    conn.close()


def row(conn, table, row_id):
    result = conn.execute(f'select * from "{table}" where id = ?', (row_id,)).fetchone()
    return dict(result) if result else None


def table_snapshot(conn):
    tables = [item[0] for item in conn.execute("select name from sqlite_master where type='table'")]
    return {table: [dict(item) for item in conn.execute(f'select * from "{table}" order by id')] for table in tables}


def execute_main(namespace, main, conn, restart_ok=True, active=True):
    class ConnectionProxy:
        row_factory = sqlite3.Row

        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    def run(command, **kwargs):
        code = 0 if restart_ok else 1
        if isinstance(command, list) and "is-active" in command:
            code = 0 if active else 3
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    namespace["find_db_path"] = lambda: ":memory:"
    namespace["sqlite3"] = SimpleNamespace(connect=lambda path: ConnectionProxy(), Row=sqlite3.Row)
    namespace["subprocess"] = SimpleNamespace(run=run)
    namespace["time"] = SimpleNamespace(sleep=lambda _: None, strftime=lambda _: "2026-09-20", time=lambda: 1)
    exec(main, namespace)


def test_policy_update_preserves_traffic_counters(panel_db):
    namespace, _ = remote_program()
    namespace["add_client_to_db"](panel_db)
    namespace["add_client_to_db"](panel_db)
    traffic = row(panel_db, "client_traffics", 10)
    assert (traffic["up"], traffic["down"], traffic["last_online"]) == (123, 456, 789)
    assert traffic["total"] == 25 * 1024**3
    assert traffic["expiry_time"] == NEW_EXPIRY
    assert traffic["reset"] == 17


def test_policy_update_preserves_existing_client_settings(panel_db):
    namespace, _ = remote_program()
    before = json.loads(row(panel_db, "inbounds", 1)["settings"])["clients"][0]
    namespace["add_client_to_db"](panel_db)
    clients = json.loads(row(panel_db, "inbounds", 1)["settings"])["clients"]
    client = next(item for item in clients if item["id"] == CLIENT_UUID)
    expected = dict(before, limitIp=3, totalGB=25 * 1024**3, expiryTime=NEW_EXPIRY, enable=True)
    assert client == expected
    if "clients" in table_snapshot(panel_db):
        normalized = row(panel_db, "clients", 5)
        assert (normalized["uuid"], normalized["flow"], normalized["sub_id"], normalized["tg_id"], normalized["reset"]) == (
            CLIENT_UUID, "vision", "sub-5", 12345, 17,
        )
        assert (normalized["limit_ip"], normalized["total_gb"], normalized["expiry_time"]) == (3, 25 * 1024**3, NEW_EXPIRY)


def test_policy_update_preserves_inbound_relations_and_unrelated_clients(panel_db):
    namespace, _ = remote_program()
    before = table_snapshot(panel_db)
    namespace["add_client_to_db"](panel_db)
    assert row(panel_db, "inbounds", 2) == before["inbounds"][1]
    assert row(panel_db, "client_traffics", 11) == before["client_traffics"][1]
    if "clients" in before:
        assert row(panel_db, "clients", 6) == before["clients"][1]
        assert table_snapshot(panel_db)["client_inbounds"] == before["client_inbounds"]


def test_insert_initializes_zero_traffic(panel_db):
    panel_db.execute("delete from client_traffics where id = 10")
    namespace, _ = remote_program()
    namespace["add_client_to_db"](panel_db)
    traffic = dict(panel_db.execute("select * from client_traffics where email = ?", (EMAIL,)).fetchone())
    assert (traffic["up"], traffic["down"], traffic["last_online"]) == (0, 0, 0)


@pytest.mark.parametrize("operation", ["provision", "suspend"])
@pytest.mark.parametrize("restart_ok,active", [(False, True), (True, False)])
def test_failed_restart_cannot_emit_success(panel_db, operation, restart_ok, active, capsys):
    namespace, main = remote_program(operation)
    with pytest.raises(RuntimeError, match="restart|active"):
        execute_main(namespace, main, panel_db, restart_ok=restart_ok, active=active)
    output = capsys.readouterr().out
    assert "STATUS=provisioned" not in output
    assert "STATUS=suspended" not in output


def test_suspend_disables_matching_clients_without_deleting_and_resume_restores(panel_db, capsys):
    namespace, main = remote_program("suspend")
    before = table_snapshot(panel_db)
    execute_main(namespace, main, panel_db)
    assert "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended" in capsys.readouterr().out
    after = table_snapshot(panel_db)
    assert {table: len(rows) for table, rows in after.items()} == {table: len(rows) for table, rows in before.items()}
    original_settings = json.loads(before["inbounds"][0]["settings"])
    original_settings["clients"][0]["enable"] = False
    assert json.loads(after["inbounds"][0]["settings"]) == original_settings
    assert after["inbounds"][1] == before["inbounds"][1]
    assert after["client_traffics"][0] == dict(before["client_traffics"][0], enable=0)
    assert after["client_traffics"][1] == before["client_traffics"][1]
    if "clients" in before:
        assert after["clients"][0] == dict(before["clients"][0], enabled=0)
        assert after["clients"][1] == before["clients"][1]
        assert after["client_inbounds"] == before["client_inbounds"]
    execute_main(namespace, main, panel_db)
    assert table_snapshot(panel_db) == after
    provision, _ = remote_program()
    provision["add_client_to_db"](panel_db)
    client = next(item for item in json.loads(row(panel_db, "inbounds", 1)["settings"])["clients"] if item["id"] == CLIENT_UUID)
    assert client["enable"] is True
    assert row(panel_db, "client_traffics", 10)["enable"] == 1
    assert row(panel_db, "client_traffics", 10)["up"] == 123
    if "clients" in before:
        assert row(panel_db, "clients", 5)["enabled"] == 1


def test_absent_client_is_already_suspended(panel_db, capsys):
    namespace, main = remote_program("suspend")
    namespace["payload"]["client_uuid"] = "absent-uuid"
    namespace["payload"]["client_email"] = "absent-email"
    before = table_snapshot(panel_db)
    execute_main(namespace, main, panel_db)
    assert "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended" in capsys.readouterr().out
    assert table_snapshot(panel_db) == before


@pytest.mark.parametrize("panel_db", ["normalized"], indirect=True)
def test_normalized_only_password_schema_suspends_and_resumes(panel_db, capsys):
    panel_db.execute("alter table inbounds drop column settings")
    panel_db.execute("alter table clients rename column uuid to password")
    namespace, main = remote_program("suspend")
    execute_main(namespace, main, panel_db)
    assert "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended" in capsys.readouterr().out
    assert row(panel_db, "clients", 5)["enabled"] == 0
    provision, _ = remote_program()
    provision["add_client_to_db"](panel_db)
    client = row(panel_db, "clients", 5)
    assert (client["password"], client["enabled"], client["flow"], client["limit_ip"]) == (CLIENT_UUID, 1, "vision", 3)
    assert row(panel_db, "client_traffics", 10)["up"] == 123


@pytest.mark.parametrize("enable_column", ["enabled", "active"])
def test_resume_restores_traffic_enable_aliases(panel_db, enable_column):
    panel_db.execute(f'alter table client_traffics rename column enable to "{enable_column}"')
    namespace, main = remote_program("suspend")
    execute_main(namespace, main, panel_db)
    assert row(panel_db, "client_traffics", 10)[enable_column] == 0
    provision, _ = remote_program()
    provision["add_client_to_db"](panel_db)
    assert row(panel_db, "client_traffics", 10)[enable_column] == 1


def test_existing_client_sync_skips_add_client_api(panel_db):
    # Valid stored credentials would otherwise send addClient before updating SQL.
    panel_db.execute("create table settings (key text, value text)")
    panel_db.execute("insert into settings values ('webPort', '12345')")
    panel_db.execute("create table users (id integer, username text, password text)")
    panel_db.execute("insert into users values (1, 'admin', 'private-password')")
    namespace, main = remote_program()
    network_attempts = []

    class BlockNetwork:
        @staticmethod
        def build_opener(*args):
            network_attempts.append(True)
            raise RuntimeError("unexpected network call")

        HTTPCookieProcessor = staticmethod(lambda *args: None)

    namespace["urllib"] = SimpleNamespace(request=BlockNetwork(), parse=namespace["urllib"].parse)
    try:
        execute_main(namespace, main, panel_db)
    except RuntimeError:
        if not network_attempts:
            raise
    assert not network_attempts, "existing clients must be updated in place without addClient API mutation"


@pytest.mark.parametrize("panel_db", ["normalized"], indirect=True)
def test_insert_initializes_numeric_telegram_id(panel_db):
    panel_db.execute("delete from clients where id = 5")
    namespace, _ = remote_program()
    namespace["add_client_to_db"](panel_db)
    client = dict(panel_db.execute("select * from clients where uuid = ?", (CLIENT_UUID,)).fetchone())
    assert client["tg_id"] == 0
    assert row(panel_db, "clients", 6)["tg_id"] == ""


def test_lowered_quota_keeps_already_consumed_traffic(panel_db):
    panel_db.execute("update client_traffics set up = ?, down = ? where id = 10", (30 * 1024**3, 5 * 1024**3))
    namespace, _ = remote_program()
    namespace["add_client_to_db"](panel_db)
    traffic = row(panel_db, "client_traffics", 10)
    assert (traffic["up"], traffic["down"]) == (30 * 1024**3, 5 * 1024**3)
    assert traffic["total"] == 25 * 1024**3
    assert traffic["up"] + traffic["down"] > traffic["total"]


def test_traffic_update_without_id_preserves_other_inbound(panel_db):
    panel_db.execute("""create table old_traffics as select inbound_id, email, client_id,
        enable, up, down, last_online, total, expiry_time, reset from client_traffics""")
    panel_db.execute("drop table client_traffics")
    panel_db.execute("alter table old_traffics rename to client_traffics")
    panel_db.execute("insert into client_traffics values (2, ?, 999, 1, 1000, 2000, 3000, 444, 555, 8)", (EMAIL,))
    before = dict(panel_db.execute("select * from client_traffics where inbound_id = 2").fetchone())
    namespace, _ = remote_program()
    namespace["add_client_to_db"](panel_db)
    after = panel_db.execute("select * from client_traffics where inbound_id = 2").fetchone()
    assert after is not None, "policy update moved a different inbound's traffic row"
    assert dict(after) == before
