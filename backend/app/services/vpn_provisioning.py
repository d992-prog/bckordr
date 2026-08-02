from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnNodeEvent, VpnSubscription, WorkerNode
from app.services.worker_maintenance import _bash, _shell_quote, execute_worker_ssh_commands


VPN_CLIENT_MARKER_PREFIX = "DROPCATCH_VPN_CLIENT_"
VPN_CLIENT_REVOKE_MARKER_PREFIX = "DROPCATCH_VPN_CLIENT_REVOKE_"


@dataclass(frozen=True)
class VpnClientProvisionPayload:
    client_uuid: UUID
    client_email: str
    inbound_id: int
    protocol: str = "vless"
    expires_at: datetime | None = None
    traffic_limit_gb: int | None = None
    max_devices: int = 1


def build_vpn_client_email(access_key_id: int, public_name: str | None) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", (public_name or "client").lower()).strip("-")
    if not suffix:
        suffix = "client"
    return f"dropcatch-{access_key_id}-{suffix}"[:64].rstrip("-")


def ensure_vpn_client_uuid(access_key: VpnAccessKey) -> UUID:
    if access_key.external_uuid:
        try:
            return UUID(str(access_key.external_uuid))
        except ValueError:
            pass
    client_uuid = uuid4()
    access_key.external_uuid = str(client_uuid)
    return client_uuid


def parse_vpn_client_provision_output(log: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for raw_line in log.splitlines():
        line = raw_line.strip()
        if not line.startswith(VPN_CLIENT_MARKER_PREFIX) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        metadata[key.removeprefix(VPN_CLIENT_MARKER_PREFIX).lower()] = value.strip()
    return metadata


def _remote_python_script() -> str:
    return r"""
import base64
import http.cookiejar
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

payload = json.loads(os.environ["DROPCATCH_VPN_CLIENT_PAYLOAD"])


def emit(key, value):
    if value is not None and str(value).strip():
        sanitized = str(value).strip().replace("\n", " ")[:3000]
        print(f"DROPCATCH_VPN_CLIENT_{key}={sanitized}")


def table_columns(conn, table):
    try:
        return [row[1] for row in conn.execute(f'pragma table_info("{table}")').fetchall()]
    except Exception:
        return []


def table_names(conn):
    return [
        row[0]
        for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
    ]


def table_info(conn, table):
    try:
        columns = [item[0] for item in conn.execute(f'pragma table_info("{table}")').description or []]
        return [
            dict(zip(columns, row))
            for row in conn.execute(f'pragma table_info("{table}")').fetchall()
        ]
    except Exception:
        return []


def rows_as_dicts(cursor):
    columns = [item[0] for item in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def find_db_path():
    for path in ("/etc/x-ui/x-ui.db", "/usr/local/x-ui/bin/x-ui.db", "/usr/local/x-ui/x-ui.db"):
        if os.path.exists(path):
            return path
    raise RuntimeError("3x-UI database was not found")


def read_settings(conn):
    settings = {}
    if "settings" not in table_names(conn):
        return settings
    columns = table_columns(conn, "settings")
    key_column = "key" if "key" in columns else ("name" if "name" in columns else "")
    value_column = "value" if "value" in columns else ""
    if not key_column or not value_column:
        return settings
    for row in rows_as_dicts(conn.execute(f'select "{key_column}", "{value_column}" from settings')):
        settings[str(row.get(key_column))] = row.get(value_column)
    return settings


def read_user(conn):
    if "users" not in table_names(conn):
        return "", ""
    user = rows_as_dicts(conn.execute("select * from users order by id limit 1"))
    if not user:
        return "", ""
    row = user[0]
    username = next((str(row[column]) for column in ("username", "user_name", "login", "email") if row.get(column)), "")
    password = next((str(row[column]) for column in ("password", "passwd", "pass") if row.get(column)), "")
    return username, password


def build_panel_urls(settings):
    panel_url = (payload.get("panel_url") or "").rstrip("/")
    local_url = ""
    port = next((str(settings[key]) for key in ("webPort", "web_port", "port", "panel_port") if settings.get(key)), "")
    base_path = next((str(settings[key]) for key in ("webBasePath", "web_base_path", "base_path", "webPath") if settings.get(key)), "")
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    if port:
        local_url = f"http://127.0.0.1:{port}{base_path}".rstrip("/")
    return panel_url, local_url


def request_json(opener, url, body=None, method=None):
    parsed = urllib.parse.urlsplit(url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if parsed.scheme and parsed.netloc:
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return opener.open(req, timeout=10)


def request_form(opener, url, body):
    parsed = urllib.parse.urlsplit(url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    if parsed.scheme and parsed.netloc:
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(body).encode(), headers=headers)
    return opener.open(req, timeout=10)


def read_response(response, limit=900):
    return response.read().decode("utf-8", errors="replace")[:limit]


def client_settings():
    expiry = payload.get("expires_at_ms") or 0
    total_gb = payload.get("traffic_limit_gb") or 0
    return {
        "id": payload["client_uuid"],
        "email": payload["client_email"],
        "flow": "",
        "limitIp": int(payload.get("max_devices") or 1),
        "totalGB": int(total_gb) * 1024 * 1024 * 1024 if int(total_gb) > 0 else 0,
        "expiryTime": int(expiry),
        "enable": True,
        "tgId": "",
        "subId": "",
        "reset": 0,
    }


def try_api_add_client(local_url, username, password):
    if not local_url or not username or not password:
        return False, "missing local panel URL or credentials"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    last_error = ""
    for login_mode in ("form", "json"):
        try:
            if login_mode == "form":
                request_form(opener, local_url + "/login", {"username": username, "password": password})
            else:
                request_json(opener, local_url + "/login", {"username": username, "password": password})
            response = request_json(
                opener,
                local_url + "/panel/api/inbounds/addClient",
                {"id": int(payload["inbound_id"]), "settings": json.dumps({"clients": [client_settings()]})},
            )
            body = read_response(response)
            emit("API_RESULT", f"addClient HTTP {response.status}: {body}")
            if 200 <= int(response.status) < 300:
                return True, body
        except urllib.error.HTTPError as exc:
            last_error = f"{login_mode}: HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:600]}"
        except Exception as exc:
            last_error = f"{login_mode}: {exc}"
    return False, last_error


def parse_json(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def quote_identifier(column):
    return '"' + column.replace('"', '""') + '"'


def column_info_by_name(conn, table):
    return {str(item.get("name")): item for item in table_info(conn, table)}


def is_integer_primary_key(info):
    column_type = str(info.get("type") or "").upper()
    return bool(info.get("pk")) and "INT" in column_type


def total_limit_bytes():
    total_gb = int(payload.get("traffic_limit_gb") or 0)
    return total_gb * 1024 * 1024 * 1024 if total_gb > 0 else 0


def default_column_value(column, column_type):
    column_lower = column.lower()
    type_upper = str(column_type or "").upper()
    if column_lower in ("created_at", "updated_at"):
        if "INT" in type_upper:
            return int(time.time() * 1000)
        return time.strftime("%Y-%m-%d %H:%M:%S")
    if column_lower in ("enable", "enabled", "active", "reset"):
        return 1 if column_lower in ("enable", "enabled", "active") else 0
    if any(token in type_upper for token in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
        return 0
    return ""


def numeric_column_value(column_type):
    type_upper = str(column_type or "").upper()
    if any(token in type_upper for token in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
        return 0
    return ""


def values_with_required_defaults(conn, table, values):
    result = dict(values)
    for name, info in column_info_by_name(conn, table).items():
        if name in result or info.get("pk") or not info.get("notnull") or info.get("dflt_value") is not None:
            continue
        result[name] = default_column_value(name, info.get("type"))
    return result


def insert_row(conn, table, values):
    columns = table_columns(conn, table)
    prepared = {
        key: value
        for key, value in values_with_required_defaults(conn, table, values).items()
        if key in columns
    }
    if not prepared:
        return None
    column_sql = ", ".join(quote_identifier(column) for column in prepared)
    placeholder_sql = ", ".join("?" for _ in prepared)
    conn.execute(
        f"insert into {quote_identifier(table)} ({column_sql}) values ({placeholder_sql})",
        tuple(prepared.values()),
    )
    return conn.execute("select last_insert_rowid()").fetchone()[0]


def update_row(conn, table, match_column, match_value, values):
    columns = table_columns(conn, table)
    prepared = {key: value for key, value in values.items() if key in columns and key != match_column}
    if not prepared:
        return
    assignments = ", ".join(f"{quote_identifier(column)} = ?" for column in prepared)
    conn.execute(
        f"update {quote_identifier(table)} set {assignments} where {quote_identifier(match_column)} = ?",
        tuple(prepared.values()) + (match_value,),
    )


def first_matching_row(conn, table, matches):
    columns = table_columns(conn, table)
    for column, value in matches:
        if column not in columns and column.lower() != "rowid":
            continue
        if value is None:
            continue
        rows = rows_as_dicts(
            conn.execute(
                f"select * from {quote_identifier(table)} where {quote_identifier(column)} = ? order by rowid limit 1",
                (value,),
            )
        )
        if rows:
            return rows[0], column
    return None, ""


def repair_client_telegram_ids(conn):
    if "clients" not in table_names(conn):
        return ""
    columns = table_columns(conn, "clients")
    info = column_info_by_name(conn, "clients")
    repaired = 0
    for column in ("tg_id", "tgId", "telegram_id"):
        if column not in columns:
            continue
        if numeric_column_value(info.get(column, {}).get("type")) != 0:
            continue
        cursor = conn.execute(
            f"update clients set {quote_identifier(column)} = 0 "
            f"where {quote_identifier(column)} is null "
            f"or trim(cast({quote_identifier(column)} as text)) = ''"
        )
        if cursor.rowcount and cursor.rowcount > 0:
            repaired += int(cursor.rowcount)
    return f"repaired telegram ids={repaired}" if repaired else ""


def sync_clients_table(conn):
    if "clients" not in table_names(conn):
        return None, "clients table missing"

    columns = table_columns(conn, "clients")
    info = column_info_by_name(conn, "clients")
    repair_status = repair_client_telegram_ids(conn)
    id_info = info.get("id", {})
    uuid_column = next((column for column in ("uuid", "client_uuid", "client_id", "password", "passwd") if column in columns), "")
    if not uuid_column and "id" in columns and not is_integer_primary_key(id_info):
        uuid_column = "id"

    values = {}
    if uuid_column:
        values[uuid_column] = payload["client_uuid"]
    if "id" in columns and not is_integer_primary_key(id_info):
        values["id"] = payload["client_uuid"]
    for column in ("uuid", "client_uuid", "password", "passwd"):
        if column in columns:
            values[column] = payload["client_uuid"]
    if "inbound_id" in columns:
        values["inbound_id"] = int(payload["inbound_id"])
    if "email" in columns:
        values["email"] = payload["client_email"]
    if "protocol" in columns:
        values["protocol"] = payload.get("protocol") or "vless"
    for column in ("enable", "enabled", "active"):
        if column in columns:
            values[column] = 1
    for column in ("flow", "flow_override"):
        if column in columns:
            values[column] = ""
    for column in ("limit_ip", "limitIp", "limit_ips"):
        if column in columns:
            values[column] = int(payload.get("max_devices") or 1)
    for column in ("total", "total_gb", "totalGB"):
        if column in columns:
            values[column] = total_limit_bytes()
    for column in ("expiry_time", "expiryTime", "expires_at"):
        if column in columns:
            values[column] = int(payload.get("expires_at_ms") or 0)
    for column in ("tg_id", "tgId", "telegram_id"):
        if column in columns:
            values[column] = numeric_column_value(info.get(column, {}).get("type"))
    for column in ("sub_id", "subId"):
        if column in columns:
            values[column] = ""
    if "reset" in columns:
        values["reset"] = 0

    existing, match_column = first_matching_row(
        conn,
        "clients",
        [
            (uuid_column, payload["client_uuid"]),
            ("uuid", payload["client_uuid"]),
            ("client_uuid", payload["client_uuid"]),
            ("password", payload["client_uuid"]),
            ("passwd", payload["client_uuid"]),
            ("email", payload["client_email"]),
        ],
    )
    if existing:
        update_row(conn, "clients", match_column, existing[match_column], values)
        client_pk = existing.get("id") if "id" in columns else existing.get(uuid_column)
        suffix = f"; {repair_status}" if repair_status else ""
        return client_pk, f"clients updated by {match_column}{suffix}"

    client_pk = insert_row(conn, "clients", values)
    if "id" in columns:
        inserted = first_matching_row(
            conn,
            "clients",
            [
                (uuid_column, payload["client_uuid"]),
                ("email", payload["client_email"]),
                ("rowid", client_pk),
            ],
        )[0]
        if inserted:
            client_pk = inserted.get("id")
    suffix = f"; {repair_status}" if repair_status else ""
    return client_pk, f"clients inserted{suffix}"


def sync_client_inbounds_table(conn, client_pk):
    if "client_inbounds" not in table_names(conn) or client_pk is None:
        return "client_inbounds skipped"
    columns = table_columns(conn, "client_inbounds")
    values = {}
    if "client_id" in columns:
        values["client_id"] = client_pk
    if "inbound_id" in columns:
        values["inbound_id"] = int(payload["inbound_id"])
    if "flow_override" in columns:
        values["flow_override"] = ""
    if "created_at" in columns:
        values["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if not {"client_id", "inbound_id"}.issubset(values):
        return "client_inbounds unsupported columns"
    existing = []
    if {"client_id", "inbound_id"}.issubset(set(columns)):
        existing = rows_as_dicts(
            conn.execute(
                "select * from client_inbounds where client_id = ? and inbound_id = ? order by rowid limit 1",
                (client_pk, int(payload["inbound_id"])),
            )
        )
    if existing:
        match_column = "id" if "id" in columns else "client_id"
        update_row(conn, "client_inbounds", match_column, existing[0].get(match_column) or client_pk, values)
        return "client_inbounds updated"
    insert_row(conn, "client_inbounds", values)
    return "client_inbounds inserted"


def sync_client_traffics_table(conn, client_pk):
    if "client_traffics" not in table_names(conn):
        return "client_traffics table missing"
    columns = table_columns(conn, "client_traffics")
    values = {}
    if "client_id" in columns and client_pk is not None:
        values["client_id"] = client_pk
    if "inbound_id" in columns:
        values["inbound_id"] = int(payload["inbound_id"])
    if "email" in columns:
        values["email"] = payload["client_email"]
    for column in ("uuid", "client_uuid", "password", "passwd"):
        if column in columns:
            values[column] = payload["client_uuid"]
    if "enable" in columns:
        values["enable"] = 1
    for column in ("up", "down", "last_online"):
        if column in columns:
            values[column] = 0
    for column in ("total", "total_gb", "totalGB"):
        if column in columns:
            values[column] = total_limit_bytes()
    for column in ("expiry_time", "expiryTime"):
        if column in columns:
            values[column] = int(payload.get("expires_at_ms") or 0)
    if "reset" in columns:
        values["reset"] = 0
    existing = []
    if {"inbound_id", "email"}.issubset(set(columns)):
        existing = rows_as_dicts(
            conn.execute(
                "select * from client_traffics where inbound_id = ? and email = ? order by rowid limit 1",
                (int(payload["inbound_id"]), payload["client_email"]),
            )
        )
    if not existing:
        existing, _ = first_matching_row(
            conn,
            "client_traffics",
            [
                ("client_id", client_pk),
                ("uuid", payload["client_uuid"]),
                ("client_uuid", payload["client_uuid"]),
                ("password", payload["client_uuid"]),
                ("passwd", payload["client_uuid"]),
            ],
        )
        existing = [existing] if existing else []
    if existing:
        update_row(conn, "client_traffics", "id" if "id" in columns else "email", existing[0].get("id") or payload["client_email"], values)
        return "client_traffics updated"
    insert_row(conn, "client_traffics", values)
    return "client_traffics inserted"


def restart_xui():
    subprocess.run("systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service || true", shell=True, check=False)
    time.sleep(1)


def build_config_uri(inbound):
    protocol = str(inbound.get("protocol") or payload.get("protocol") or "vless")
    port = str(inbound.get("port") or payload.get("inbound_port") or "")
    host = payload.get("public_host") or payload.get("ssh_host") or ""
    settings = parse_json(inbound.get("settings"), {})
    stream = parse_json(inbound.get("streamSettings") or inbound.get("stream_settings"), {})
    clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
    client = next((item for item in clients if str(item.get("id")) == payload["client_uuid"]), {})
    network = str(stream.get("network") or payload.get("inbound_transport") or "tcp").lower()
    security = str(stream.get("security") or payload.get("inbound_security") or "none").lower()
    params = {"type": network, "security": security}
    if protocol == "vless":
        params["encryption"] = "none"
    if client.get("flow"):
        params["flow"] = str(client["flow"])
    tcp_settings = stream.get("tcpSettings") or stream.get("tcp_settings") or {}
    if network == "tcp":
        tcp_header = tcp_settings.get("header") if isinstance(tcp_settings.get("header"), dict) else {}
        params["headerType"] = str(tcp_header.get("type") or "none").lower()
    ws_settings = stream.get("wsSettings") or stream.get("ws_settings") or {}
    if network == "ws":
        if ws_settings.get("path"):
            params["path"] = str(ws_settings["path"])
        ws_headers = ws_settings.get("headers") or {}
        if ws_headers.get("Host"):
            params["host"] = str(ws_headers["Host"])
    grpc_settings = stream.get("grpcSettings") or stream.get("grpc_settings") or {}
    if network == "grpc" and grpc_settings.get("serviceName"):
        params["serviceName"] = str(grpc_settings["serviceName"])
    reality_settings = stream.get("realitySettings") or stream.get("reality_settings") or {}
    if security == "reality":
        if reality_settings.get("publicKey"):
            params["pbk"] = str(reality_settings["publicKey"])
        if reality_settings.get("shortIds"):
            short_ids = reality_settings.get("shortIds") or []
            if isinstance(short_ids, list) and short_ids:
                params["sid"] = str(short_ids[0])
        if reality_settings.get("serverNames"):
            server_names = reality_settings.get("serverNames") or []
            if isinstance(server_names, list) and server_names:
                params["sni"] = str(server_names[0])
        if reality_settings.get("spiderX"):
            params["spx"] = str(reality_settings["spiderX"])
        if reality_settings.get("fingerprint"):
            params["fp"] = str(reality_settings["fingerprint"])
    tls_settings = stream.get("tlsSettings") or stream.get("tls_settings") or {}
    if security == "tls":
        if tls_settings.get("serverName"):
            params["sni"] = str(tls_settings["serverName"])
        if tls_settings.get("alpn"):
            alpn = tls_settings.get("alpn")
            if isinstance(alpn, list):
                params["alpn"] = ",".join(str(item) for item in alpn)
            else:
                params["alpn"] = str(alpn)
    if not port:
        return ""
    query = urllib.parse.urlencode(params)
    label = urllib.parse.quote(payload["client_email"])
    if protocol == "vmess":
        vmess_ws_headers = ws_settings.get("headers") if isinstance(ws_settings.get("headers"), dict) else {}
        vmess = {
            "v": "2",
            "ps": payload["client_email"],
            "add": host,
            "port": port,
            "id": payload["client_uuid"],
            "aid": "0",
            "net": network,
            "type": "none",
            "host": str(vmess_ws_headers.get("Host") or ""),
            "path": str(ws_settings.get("path") or grpc_settings.get("serviceName") or ""),
            "tls": "" if security == "none" else security,
        }
        return "vmess://" + base64.urlsafe_b64encode(json.dumps(vmess, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{protocol}://{payload['client_uuid']}@{host}:{port}?{query}#{label}"


def add_client_to_db(conn):
    inbound_rows = rows_as_dicts(conn.execute("select * from inbounds where id = ?", (int(payload["inbound_id"]),)))
    if not inbound_rows:
        raise RuntimeError(f"inbound id {payload['inbound_id']} was not found")
    inbound = inbound_rows[0]
    columns = table_columns(conn, "inbounds")
    if "settings" in columns:
        settings = parse_json(inbound.get("settings"), {})
        clients = settings.get("clients")
        if not isinstance(clients, list):
            clients = []
        client = client_settings()
        clients = [
            current
            for current in clients
            if str(current.get("id")) != payload["client_uuid"] and str(current.get("email")) != payload["client_email"]
        ]
        clients.append(client)
        settings["clients"] = clients
        conn.execute("update inbounds set settings = ? where id = ?", (json.dumps(settings, separators=(",", ":")), int(payload["inbound_id"])))
        emit("DB_INBOUNDS", "settings updated")
    else:
        emit("DB_INBOUNDS", "settings column missing; using normalized client tables")

    client_pk, clients_status = sync_clients_table(conn)
    relation_status = sync_client_inbounds_table(conn, client_pk)
    traffic_status = sync_client_traffics_table(conn, client_pk)
    emit("DB_CLIENTS", clients_status)
    emit("DB_CLIENT_INBOUNDS", relation_status)
    emit("DB_CLIENT_TRAFFICS", traffic_status)
    conn.commit()
    return build_config_uri(inbound)


db_path = find_db_path()
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
try:
    settings = read_settings(conn)
    username, password = read_user(conn)
    _, local_url = build_panel_urls(settings)
    emit("DB", db_path)
    api_ok, api_result = try_api_add_client(local_url, username, password)
    if not api_ok:
        emit("API_ERROR", api_result)
    uri = add_client_to_db(conn)
    restart_xui()
    if not uri:
        inbound = rows_as_dicts(conn.execute("select * from inbounds where id = ?", (int(payload["inbound_id"]),)))[0]
        uri = build_config_uri(inbound)
    print("DROPCATCH_VPN_CLIENT_STATUS=provisioned")
    print("DROPCATCH_VPN_CLIENT_ID=" + payload["client_uuid"])
    print("DROPCATCH_VPN_CLIENT_EMAIL=" + payload["client_email"])
    print("DROPCATCH_VPN_CLIENT_URL=" + uri)
finally:
    conn.close()
"""


def _remote_client_revoke_script() -> str:
    return r"""
import json
import os
import sqlite3
import subprocess
import time

payload = json.loads(os.environ["DROPCATCH_VPN_CLIENT_REVOKE_PAYLOAD"])


def emit(key, value):
    if value is not None and str(value).strip():
        sanitized = str(value).strip().replace("\n", " ")[:3000]
        print(f"DROPCATCH_VPN_CLIENT_REVOKE_{key}={sanitized}")


def table_names(conn):
    return [
        row[0]
        for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
    ]


def table_columns(conn, table):
    try:
        return [row[1] for row in conn.execute(f'pragma table_info("{table}")').fetchall()]
    except Exception:
        return []


def rows_as_dicts(cursor):
    columns = [item[0] for item in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def parse_json(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def find_db_path():
    for path in ("/etc/x-ui/x-ui.db", "/usr/local/x-ui/bin/x-ui.db", "/usr/local/x-ui/x-ui.db"):
        if os.path.exists(path):
            return path
    raise RuntimeError("3x-UI database was not found")


def find_client_pk(conn):
    if "clients" not in table_names(conn):
        return None
    columns = table_columns(conn, "clients")
    conditions = []
    params = []
    for column in ("uuid", "client_uuid", "id", "password", "passwd"):
        if column in columns and payload.get("client_uuid"):
            conditions.append(f'"{column}" = ?')
            params.append(payload["client_uuid"])
    if "email" in columns and payload.get("client_email"):
        conditions.append('"email" = ?')
        params.append(payload["client_email"])
    if not conditions:
        return None
    rows = rows_as_dicts(conn.execute("select * from clients where " + " or ".join(conditions) + " limit 1", params))
    if not rows:
        return None
    return rows[0].get("id") or rows[0].get("client_id") or rows[0].get("uuid") or payload.get("client_uuid")


def delete_matching(conn, table, pairs):
    if table not in table_names(conn):
        return 0
    columns = table_columns(conn, table)
    conditions = []
    params = []
    for column, value in pairs:
        if value in (None, ""):
            continue
        if column != "rowid" and column not in columns:
            continue
        quoted = "rowid" if column == "rowid" else f'"{column}"'
        conditions.append(f"{quoted} = ?")
        params.append(value)
    if not conditions:
        return 0
    cursor = conn.execute(f'delete from "{table}" where ' + " or ".join(conditions), params)
    return int(cursor.rowcount or 0)


def remove_from_inbound_settings(conn):
    if "inbounds" not in table_names(conn):
        return "inbounds table missing"
    columns = table_columns(conn, "inbounds")
    if "settings" not in columns:
        return "settings column missing"
    rows = rows_as_dicts(conn.execute("select * from inbounds where id = ?", (int(payload["inbound_id"]),)))
    if not rows:
        return "inbound missing"
    inbound = rows[0]
    settings = parse_json(inbound.get("settings"), {})
    clients = settings.get("clients")
    if not isinstance(clients, list):
        return "clients list missing"
    before = len(clients)
    settings["clients"] = [
        client
        for client in clients
        if str(client.get("id")) != str(payload.get("client_uuid"))
        and str(client.get("email")) != str(payload.get("client_email"))
    ]
    conn.execute(
        "update inbounds set settings = ? where id = ?",
        (json.dumps(settings, separators=(",", ":")), int(payload["inbound_id"])),
    )
    return f"removed={before - len(settings['clients'])}"


def restart_xui():
    subprocess.run("systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service || true", shell=True, check=False)
    time.sleep(1)


db_path = find_db_path()
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
try:
    client_pk = find_client_pk(conn)
    emit("DB", db_path)
    emit("CLIENT_PK", client_pk)
    emit("INBOUNDS", remove_from_inbound_settings(conn))
    traffic_deleted = delete_matching(
        conn,
        "client_traffics",
        [
            ("client_id", client_pk),
            ("uuid", payload.get("client_uuid")),
            ("client_uuid", payload.get("client_uuid")),
            ("password", payload.get("client_uuid")),
            ("passwd", payload.get("client_uuid")),
            ("email", payload.get("client_email")),
        ],
    )
    relation_deleted = delete_matching(conn, "client_inbounds", [("client_id", client_pk)])
    clients_deleted = delete_matching(
        conn,
        "clients",
        [
            ("id", client_pk),
            ("uuid", payload.get("client_uuid")),
            ("client_uuid", payload.get("client_uuid")),
            ("password", payload.get("client_uuid")),
            ("passwd", payload.get("client_uuid")),
            ("email", payload.get("client_email")),
        ],
    )
    conn.commit()
    restart_xui()
    emit("CLIENT_TRAFFICS_DELETED", traffic_deleted)
    emit("CLIENT_INBOUNDS_DELETED", relation_deleted)
    emit("CLIENTS_DELETED", clients_deleted)
    print("DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked")
finally:
    conn.close()
"""


def build_vpn_client_provision_command(worker: WorkerNode, payload: VpnClientProvisionPayload) -> str:
    expires_at_ms = int(payload.expires_at.timestamp() * 1000) if payload.expires_at else 0
    remote_payload = {
        "client_uuid": str(payload.client_uuid),
        "client_email": payload.client_email,
        "inbound_id": payload.inbound_id,
        "protocol": payload.protocol,
        "inbound_port": worker.vpn_inbound_port or 443,
        "inbound_transport": worker.vpn_inbound_transport or "tcp",
        "inbound_security": worker.vpn_inbound_security or "none",
        "expires_at_ms": expires_at_ms,
        "traffic_limit_gb": payload.traffic_limit_gb,
        "max_devices": payload.max_devices,
        "public_host": worker.vpn_public_host or worker.ssh_host or worker.ip_address or "",
        "ssh_host": worker.ssh_host or worker.ip_address or "",
        "panel_url": worker.vpn_panel_url or "",
    }
    return _bash(
        "\n".join(
            [
                "set -e",
                f"export DROPCATCH_VPN_CLIENT_PAYLOAD={_shell_quote(json.dumps(remote_payload, separators=(',', ':')))}",
                "python3 - <<'PY'",
                _remote_python_script(),
                "PY",
            ]
        )
    )


def build_vpn_client_revoke_command(worker: WorkerNode, access_key: VpnAccessKey) -> str:
    remote_payload = {
        "client_uuid": access_key.external_uuid or "",
        "client_email": build_vpn_client_email(access_key.id, access_key.public_name),
        "inbound_id": worker.vpn_inbound_id or 0,
    }
    return _bash(
        "\n".join(
            [
                "set -e",
                f"export DROPCATCH_VPN_CLIENT_REVOKE_PAYLOAD={_shell_quote(json.dumps(remote_payload, separators=(',', ':')))}",
                "python3 - <<'PY'",
                _remote_client_revoke_script(),
                "PY",
            ]
        )
    )


async def provision_vpn_access_key(
    db: AsyncSession,
    access_key: VpnAccessKey,
    *,
    subscription: VpnSubscription | None = None,
    worker: WorkerNode | None = None,
) -> VpnAccessKey:
    now = utcnow()
    subscription = subscription or await db.get(VpnSubscription, access_key.subscription_id)
    worker = worker or (await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None)
    access_key.last_synced_at = now
    access_key.updated_at = now
    if subscription is None:
        access_key.status = "pending_sync"
        access_key.last_error = "VPN subscription was not found"
        return access_key
    if worker is None:
        access_key.status = "pending_sync"
        access_key.last_error = "VPN node is not selected"
        return access_key
    if not worker.ssh_access_configured:
        access_key.status = "pending_sync"
        access_key.last_error = "Worker SSH access is not configured"
        return access_key
    if not worker.vpn_inbound_id:
        access_key.status = "pending_sync"
        access_key.last_error = "VPN inbound ID is not configured for this worker"
        return access_key

    client_uuid = ensure_vpn_client_uuid(access_key)
    if not access_key.issued_at:
        access_key.issued_at = now
    access_key.expires_at = subscription.expires_at
    access_key.status = "syncing"
    access_key.last_error = None
    await db.flush()

    payload = VpnClientProvisionPayload(
        client_uuid=client_uuid,
        client_email=build_vpn_client_email(access_key.id, access_key.public_name),
        inbound_id=worker.vpn_inbound_id,
        protocol=access_key.protocol or "vless",
        expires_at=subscription.expires_at,
        traffic_limit_gb=subscription.traffic_limit_gb,
        max_devices=subscription.max_devices,
    )
    command = build_vpn_client_provision_command(worker, payload)
    try:
        log = await execute_worker_ssh_commands(worker, [command])
        metadata = parse_vpn_client_provision_output(log)
        config_uri = metadata.get("url")
        if not config_uri:
            raise RuntimeError(f"VPN client was created without config URL; log={log[-1200:]}")
        access_key.config_uri = config_uri
        access_key.status = "active"
        access_key.last_synced_at = utcnow()
        access_key.last_error = None
        worker.vpn_last_checked_at = access_key.last_synced_at
        worker.vpn_runtime_status = "ready"
        worker.vpn_last_error = None
        db.add(
            VpnNodeEvent(
                worker_id=worker.id,
                event_type="client_provisioned",
                level="info",
                message=f"VPN client {payload.client_email} provisioned for access key #{access_key.id}",
                details={"access_key_id": access_key.id, "client_email": payload.client_email},
            )
        )
    except Exception as exc:
        access_key.status = "pending_sync"
        access_key.last_synced_at = utcnow()
        access_key.last_error = str(exc)[:2000]
        worker.vpn_last_checked_at = access_key.last_synced_at
        worker.vpn_last_error = access_key.last_error
        db.add(
            VpnNodeEvent(
                worker_id=worker.id,
                event_type="client_provision_failed",
                level="error",
                message=f"VPN client provisioning failed for access key #{access_key.id}: {access_key.last_error[:500]}",
                details={"access_key_id": access_key.id},
            )
        )
    access_key.updated_at = utcnow()
    worker.updated_at = access_key.updated_at
    return access_key


async def revoke_vpn_access_key(
    db: AsyncSession,
    access_key: VpnAccessKey,
    *,
    worker: WorkerNode | None = None,
) -> None:
    worker = worker or (await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None)
    if worker is None or not worker.ssh_access_configured or not worker.vpn_inbound_id:
        return

    command = build_vpn_client_revoke_command(worker, access_key)
    try:
        log = await execute_worker_ssh_commands(worker, [command])
        if "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked" not in log:
            raise RuntimeError(f"VPN client revoke did not confirm success; log={log[-1200:]}")
        now = utcnow()
        access_key.revoked_at = now
        access_key.updated_at = now
        worker.vpn_last_checked_at = now
        worker.vpn_last_error = None
        worker.updated_at = now
        db.add(
            VpnNodeEvent(
                worker_id=worker.id,
                event_type="client_revoked",
                level="info",
                message=f"VPN client for access key #{access_key.id} removed from node",
                details={"access_key_id": access_key.id},
            )
        )
    except Exception as exc:
        worker.vpn_last_checked_at = utcnow()
        worker.vpn_last_error = str(exc)[:2000]
        db.add(
            VpnNodeEvent(
                worker_id=worker.id,
                event_type="client_revoke_failed",
                level="error",
                message=f"VPN client revoke failed for access key #{access_key.id}: {worker.vpn_last_error[:500]}",
                details={"access_key_id": access_key.id},
            )
        )
        raise
