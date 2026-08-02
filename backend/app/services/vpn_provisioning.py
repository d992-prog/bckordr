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
    if "settings" not in [row[0] for row in conn.execute("select name from sqlite_master where type='table'")]:
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
    if "users" not in [row[0] for row in conn.execute("select name from sqlite_master where type='table'")]:
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


def restart_xui():
    subprocess.run("systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service || true", shell=True, check=False)
    time.sleep(1)


def build_config_uri(inbound):
    protocol = str(inbound.get("protocol") or payload.get("protocol") or "vless")
    port = str(inbound.get("port") or "")
    host = payload.get("public_host") or payload.get("ssh_host") or ""
    settings = parse_json(inbound.get("settings"), {})
    stream = parse_json(inbound.get("streamSettings") or inbound.get("stream_settings"), {})
    clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
    client = next((item for item in clients if str(item.get("id")) == payload["client_uuid"]), {})
    network = str(stream.get("network") or "tcp").lower()
    security = str(stream.get("security") or "none").lower()
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
    if "settings" not in columns:
        raise RuntimeError("inbounds.settings column was not found; cannot update this 3x-UI schema automatically")
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


def build_vpn_client_provision_command(worker: WorkerNode, payload: VpnClientProvisionPayload) -> str:
    expires_at_ms = int(payload.expires_at.timestamp() * 1000) if payload.expires_at else 0
    remote_payload = {
        "client_uuid": str(payload.client_uuid),
        "client_email": payload.client_email,
        "inbound_id": payload.inbound_id,
        "protocol": payload.protocol,
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
