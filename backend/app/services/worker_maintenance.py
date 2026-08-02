from __future__ import annotations

import shlex
from collections.abc import Mapping

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import VpnNodeEvent, WorkerMaintenanceJob, WorkerNode
from app.db.session import AsyncSessionLocal
from app.services.app_settings import DiscoveryRuntimeSettings, get_discovery_runtime_settings

VPN_MAINTENANCE_ACTIONS = {
    "vpn_check",
    "vpn_install",
    "vpn_update",
    "vpn_restart",
    "vpn_autoconfig",
    "vpn_create_inbound",
}
VPN_RUNNING_STATUS_BY_ACTION = {
    "vpn_check": "checking",
    "vpn_install": "installing",
    "vpn_update": "updating",
    "vpn_restart": "restarting",
    "vpn_autoconfig": "autoconfiguring",
    "vpn_create_inbound": "autoconfiguring",
}
VPN_AUTOCONFIG_KEYS = {
    "public_host",
    "panel_url",
    "panel_username",
    "inbound_id",
    "inbound_port",
    "inbound_protocol",
    "inbound_transport",
    "inbound_security",
    "listener_status",
    "xui_active",
    "db_diagnostic",
    "inbound_candidates",
    "inbound_rows",
    "api_token_source",
    "api_token_status",
    "inbound_create_auth",
    "inbound_create_status",
    "inbound_create_error",
    "autoconfig_db_error",
}


def _shell_quote(value: str | int | float | bool) -> str:
    return shlex.quote(str(value))


def _build_printf_command(path: str, lines: list[str]) -> str:
    quoted_lines = " ".join(_shell_quote(line) for line in lines)
    return f"printf '%s\\n' {quoted_lines} > {_shell_quote(path)}"


def _build_worker_env_lines(
    worker: WorkerNode,
    *,
    runtime_base_url: str,
    simulate_mode: bool,
    discovery_settings: DiscoveryRuntimeSettings | None = None,
) -> list[str]:
    settings = get_settings()
    worker_discovery_enabled = (
        discovery_settings.discovery_worker_enabled if discovery_settings is not None else settings.discovery_worker_enabled
    )
    worker_discovery_concurrency = (
        discovery_settings.worker_discovery_concurrency
        if discovery_settings is not None
        else settings.worker_discovery_concurrency
    )
    worker_discovery_poll_interval_seconds = (
        discovery_settings.worker_discovery_poll_interval_seconds
        if discovery_settings is not None
        else settings.worker_discovery_poll_interval_seconds
    )
    return [
        f"CONTROL_BASE_URL={runtime_base_url.rstrip('/')}",
        f"WORKER_ID={worker.id}",
        f"CONTROL_TOKEN={worker.control_token or ''}",
        "POLL_INTERVAL_SECONDS=2",
        "HEARTBEAT_INTERVAL_SECONDS=5",
        "REQUEST_TIMEOUT_SECONDS=10",
        "CONNECT_TIMEOUT_SECONDS=2",
        f"SIMULATE_MODE={'true' if simulate_mode else 'false'}",
        "SIMULATE_LATENCY_MS=20",
        "SIMULATE_JITTER_MS=10",
        "SIMULATE_SUCCESS_RATE=1.0",
        "SIMULATE_SUCCESS_STATUS_CODE=200",
        "SIMULATE_FAILURE_STATUS_CODE=503",
        "SIMULATE_RANDOM_SEED=12345",
        "GANDI_CREATE_STATUS_POLL_ENABLED=false",
        "GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5",
        "GANDI_STATUS_POLL_MAX_ATTEMPTS=8",
        "REGISTRATION_CONCURRENCY_MULTIPLIER=8",
        "REGISTRATION_MAX_CONCURRENCY=160",
        f"DISCOVERY_WORKER_ENABLED={'true' if worker_discovery_enabled else 'false'}",
        f"DISCOVERY_WORKER_CONCURRENCY={worker_discovery_concurrency}",
        f"DISCOVERY_WORKER_POLL_INTERVAL_SECONDS={worker_discovery_poll_interval_seconds}",
        "MAX_IDLE_BACKOFF_SECONDS=10",
    ]


def _build_env_upsert_command(path: str, key: str, value: str | int | float | bool) -> str:
    line = f"{key}={value}"
    quoted_path = _shell_quote(path)
    quoted_line = _shell_quote(line)
    quoted_pattern = _shell_quote(f"^{key}=")
    quoted_replacement = _shell_quote(f"s|^{key}=.*|{line}|")
    return f"grep -q {quoted_pattern} {quoted_path} && sed -i {quoted_replacement} {quoted_path} || printf '%s\\n' {quoted_line} >> {quoted_path}"


def _build_worker_discovery_env_commands(discovery_settings: DiscoveryRuntimeSettings | None = None) -> list[str]:
    settings = get_settings()
    worker_discovery_enabled = (
        discovery_settings.discovery_worker_enabled if discovery_settings is not None else settings.discovery_worker_enabled
    )
    worker_discovery_concurrency = (
        discovery_settings.worker_discovery_concurrency
        if discovery_settings is not None
        else settings.worker_discovery_concurrency
    )
    worker_discovery_poll_interval_seconds = (
        discovery_settings.worker_discovery_poll_interval_seconds
        if discovery_settings is not None
        else settings.worker_discovery_poll_interval_seconds
    )
    env_path = "/opt/domain-drop-catcher/worker/.env"
    return [
        _build_env_upsert_command(env_path, "GANDI_CREATE_STATUS_POLL_ENABLED", "false"),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_ENABLED", "true" if worker_discovery_enabled else "false"),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_CONCURRENCY", worker_discovery_concurrency),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_POLL_INTERVAL_SECONDS", worker_discovery_poll_interval_seconds),
    ]


def _build_worker_service_command() -> str:
    lines = [
        "[Unit]",
        "Description=Domain Drop Catcher Worker",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=/opt/domain-drop-catcher/worker",
        "ExecStart=/opt/domain-drop-catcher/worker/.venv/bin/python -m app.main",
        "Restart=always",
        "RestartSec=2",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return _build_printf_command("/etc/systemd/system/domain-drop-worker.service", lines)


def _bash(command: str) -> str:
    return f"bash -lc {_shell_quote(command)}"


def _build_vpn_status_command() -> str:
    return _bash(
        "systemctl is-active x-ui.service "
        "|| systemctl is-active x-ui "
        "|| systemctl is-active 3x-ui.service "
        "|| true"
    )


def _build_vpn_ready_command() -> str:
    return _bash(
        "systemctl is-active x-ui.service "
        "|| systemctl is-active x-ui "
        "|| systemctl is-active 3x-ui.service"
    )


def _build_vpn_autoconfig_command(worker: WorkerNode | None) -> str:
    host = ""
    existing_panel_url = ""
    existing_inbound_id = ""
    if worker is not None:
        host = worker.vpn_public_host or worker.ssh_host or worker.ip_address or ""
        existing_panel_url = worker.vpn_panel_url or ""
        existing_inbound_id = str(worker.vpn_inbound_id or "")
    return _bash(
        "\n".join(
            [
                "set -e",
                f"export DROPCATCH_HOST={_shell_quote(host)}",
                f"export DROPCATCH_EXISTING_PANEL_URL={_shell_quote(existing_panel_url)}",
                f"export DROPCATCH_EXISTING_INBOUND_ID={_shell_quote(existing_inbound_id)}",
                "echo DROPCATCH_VPN_AUTOCONFIG_BEGIN",
                'printf "DROPCATCH_VPN_PUBLIC_HOST=%s\\n" "$DROPCATCH_HOST"',
                "ACTIVE=$(systemctl is-active x-ui.service 2>/dev/null || systemctl is-active x-ui 2>/dev/null || systemctl is-active 3x-ui.service 2>/dev/null || true)",
                'printf "DROPCATCH_VPN_XUI_ACTIVE=%s\\n" "$ACTIVE"',
                "python3 - <<'PY'",
                "import json, os, sqlite3, subprocess",
                "",
                "host = os.environ.get('DROPCATCH_HOST', '')",
                "existing_panel_url = os.environ.get('DROPCATCH_EXISTING_PANEL_URL', '')",
                "existing_inbound_id = os.environ.get('DROPCATCH_EXISTING_INBOUND_ID', '')",
                "db_paths = [",
                "    '/etc/x-ui/x-ui.db',",
                "    '/usr/local/x-ui/bin/x-ui.db',",
                "    '/usr/local/x-ui/x-ui.db',",
                "]",
                "",
                "def emit(key, value):",
                "    if value is not None and str(value).strip():",
                "        sanitized = str(value).strip().replace('\\n', ' ')[:1200]",
                "        print(f'DROPCATCH_VPN_{key}={sanitized}')",
                "",
                "def rows_as_dicts(cursor):",
                "    columns = [item[0] for item in cursor.description or []]",
                "    return [dict(zip(columns, row)) for row in cursor.fetchall()]",
                "",
                "def parse_json_object(value):",
                "    if not value:",
                "        return {}",
                "    try:",
                "        parsed = json.loads(str(value))",
                "    except Exception:",
                "        return {}",
                "    return parsed if isinstance(parsed, dict) else {}",
                "",
                "def detect_inbound_details(conn, tables, inbound_id):",
                "    if 'inbounds' not in tables:",
                "        return {}",
                "    quoted_table = '\"inbounds\"'",
                "    columns = [row[1] for row in conn.execute(f'pragma table_info({quoted_table})')]",
                "    if 'id' not in columns:",
                "        return {}",
                "    args = []",
                "    where_parts = []",
                "    if inbound_id and str(inbound_id).isdigit():",
                "        where_parts.append('id = ?')",
                "        args.append(int(inbound_id))",
                "    for enabled_column in ('enable', 'enabled'):",
                "        if enabled_column in columns:",
                "            where_parts.append(f'coalesce(\"{enabled_column}\", 1) != 0')",
                "            break",
                "    where_clause = (' where ' + ' and '.join(where_parts)) if where_parts else ''",
                "    rows = rows_as_dicts(conn.execute(f'select * from {quoted_table}{where_clause} order by id limit 1', args))",
                "    if not rows:",
                "        return {}",
                "    row = rows[0]",
                "    details = {}",
                "    if row.get('id') is not None:",
                "        details['id'] = str(row['id'])",
                "    if row.get('port') is not None:",
                "        details['port'] = str(row['port'])",
                "    if row.get('protocol') is not None:",
                "        details['protocol'] = str(row['protocol']).lower()",
                "    stream_raw = ''",
                "    for key in ('streamSettings', 'stream_settings', 'stream_settings_json', 'stream'):",
                "        if row.get(key):",
                "            stream_raw = str(row[key])",
                "            break",
                "    stream = parse_json_object(stream_raw)",
                "    if stream:",
                "        network = stream.get('network') or stream.get('net')",
                "        security = stream.get('security')",
                "        if network:",
                "            details['transport'] = str(network).lower()",
                "        if security:",
                "            details['security'] = str(security).lower()",
                "    return details",
                "",
                "def detect_listener_status(port):",
                "    if not str(port or '').isdigit():",
                "        return ''",
                "    try:",
                "        output = subprocess.check_output(['ss', '-lnt'], stderr=subprocess.STDOUT, text=True, timeout=5)",
                "    except Exception as exc:",
                "        return f'unknown:{exc}'",
                "    port_token = ':' + str(port)",
                "    for line in output.splitlines():",
                "        if 'LISTEN' in line.upper() and port_token in line:",
                "            return 'listening'",
                "    return 'not_listening'",
                "",
                "def detect_inbound_id(conn, tables):",
                "    candidate_tables = []",
                "    table_notes = []",
                "    row_notes = []",
                "",
                "    def summarize_rows(table, columns):",
                "        quoted_table = '\"' + table.replace('\"', '\"\"') + '\"'",
                "        summary = []",
                "        try:",
                "            total = conn.execute(f'select count(*) from {quoted_table}').fetchone()[0]",
                "            summary.append(f'rows={total}')",
                "        except Exception as exc:",
                "            return f'rows_error={exc}'",
                "        for enabled_column in ('enable', 'enabled'):",
                "            if enabled_column in columns:",
                "                quoted_enabled = '\"' + enabled_column.replace('\"', '\"\"') + '\"'",
                "                try:",
                "                    enabled = conn.execute(f'select count(*) from {quoted_table} where coalesce({quoted_enabled}, 1) != 0').fetchone()[0]",
                "                    summary.append(f'enabled={enabled}')",
                "                except Exception:",
                "                    pass",
                "                break",
                "        id_column = next((column for column in ('id', 'inbound_id', 'inboundId') if column in columns), None)",
                "        if id_column:",
                "            quoted_id = '\"' + id_column.replace('\"', '\"\"') + '\"'",
                "            try:",
                "                ids = [str(row[0]) for row in conn.execute(f'select {quoted_id} from {quoted_table} order by {quoted_id} limit 5').fetchall()]",
                "                if ids:",
                "                    summary.append('ids=' + ','.join(ids))",
                "            except Exception:",
                "                pass",
                "        return ','.join(summary)",
                "",
                "    for name in sorted(tables):",
                "        lowered = name.lower()",
                "        quoted_table = '\"' + name.replace('\"', '\"\"') + '\"'",
                "        try:",
                "            columns = [row[1] for row in conn.execute(f'pragma table_info({quoted_table})')]",
                "        except Exception:",
                "            continue",
                "        lowered_columns = {column.lower() for column in columns}",
                "        inbound_score = 0",
                "        if 'inbound' in lowered:",
                "            inbound_score += 3",
                "        for feature in ('port', 'protocol', 'settings', 'stream_settings', 'remark', 'enable', 'enabled', 'listen'):",
                "            if feature in lowered_columns:",
                "                inbound_score += 1",
                "        if lowered == 'inbounds':",
                "            inbound_score += 5",
                "        if inbound_score >= 3 and any(column in lowered_columns for column in ('id', 'inbound_id', 'inboundid')):",
                "            candidate_tables.append((inbound_score, name, columns))",
                "    candidate_tables.sort(key=lambda item: (-item[0], item[1]))",
                "    for _, table, columns in candidate_tables:",
                "        row_notes.append(f'{table}:{summarize_rows(table, columns)}')",
                "    for _, table, columns in candidate_tables:",
                "        quoted_table = '\"' + table.replace('\"', '\"\"') + '\"'",
                "        for column in ('id', 'inbound_id', 'inboundId'):",
                "            if column not in columns:",
                "                continue",
                "            quoted_column = '\"' + column.replace('\"', '\"\"') + '\"'",
                "            order_by = quoted_column",
                "            where_parts = [f'{quoted_column} is not null']",
                "            for enabled_column in ('enable', 'enabled'):",
                "                if enabled_column in columns:",
                "                    quoted_enabled = '\"' + enabled_column.replace('\"', '\"\"') + '\"'",
                "                    where_parts.append(f'coalesce({quoted_enabled}, 1) != 0')",
                "                    break",
                "            try:",
                "                row = conn.execute(",
                "                    f'select {quoted_column} from {quoted_table} where ' + ' and '.join(where_parts) + f' order by {order_by} limit 1'",
                "                ).fetchone()",
                "            except Exception:",
                "                continue",
                "            table_notes.append(f'{table}.{column}')",
                "            if row and str(row[0]).strip().isdigit():",
                "                return str(row[0]).strip(), ','.join(table_notes[:20]), ';'.join(row_notes[:20])",
                "    return '', ';'.join(table + ':' + ','.join(columns[:8]) for _, table, columns in candidate_tables[:8]), ';'.join(row_notes[:20])",
                "",
                "settings = {}",
                "username = ''",
                "inbound_id = existing_inbound_id",
                "inbound_details = {}",
                "db_diagnostics = []",
                "inbound_candidates = ''",
                "inbound_rows = ''",
                "for path in db_paths:",
                "    if not os.path.exists(path):",
                "        continue",
                "    try:",
                "        conn = sqlite3.connect(path)",
                "        try:",
                "            tables = {row[0] for row in conn.execute(\"select name from sqlite_master where type='table'\")}",
                "            db_diagnostics.append(path + ': tables=' + ','.join(sorted(tables)))",
                "            if 'settings' in tables and not settings:",
                "                cur = conn.execute('select * from settings')",
                "                for row in rows_as_dicts(cur):",
                "                    key = row.get('key') or row.get('name') or row.get('setting') or row.get('item')",
                "                    value = row.get('value') if 'value' in row else row.get('val')",
                "                    if key is not None and value is not None:",
                "                        settings[str(key)] = str(value)",
                "            if 'users' in tables and not username:",
                "                cur = conn.execute('select * from users order by id limit 1')",
                "                users = rows_as_dicts(cur)",
                "                if users:",
                "                    for column in ('username', 'user_name', 'login', 'email'):",
                "                        if users[0].get(column):",
                "                            username = str(users[0][column])",
                "                            break",
                "            if not inbound_id:",
                "                detected_inbound_id, inbound_candidates, detected_inbound_rows = detect_inbound_id(conn, tables)",
                "                inbound_id = detected_inbound_id or inbound_id",
                "                inbound_rows = detected_inbound_rows or inbound_rows",
                "            if not inbound_details:",
                "                inbound_details = detect_inbound_details(conn, tables, inbound_id)",
                "                if not inbound_id and inbound_details.get('id'):",
                "                    inbound_id = inbound_details['id']",
                "        finally:",
                "            conn.close()",
                "    except Exception as exc:",
                "        emit('AUTOCONFIG_DB_ERROR', f'{path}: {exc}')",
                "    if settings and username and inbound_id:",
                "        break",
                "emit('DB_DIAGNOSTIC', ' | '.join(db_diagnostics))",
                "emit('INBOUND_CANDIDATES', inbound_candidates)",
                "emit('INBOUND_ROWS', inbound_rows)",
                "",
                "port = ''",
                "for key in ('webPort', 'web_port', 'port', 'panel_port'):",
                "    if settings.get(key):",
                "        port = settings[key]",
                "        break",
                "base_path = ''",
                "for key in ('webBasePath', 'web_base_path', 'base_path', 'webPath'):",
                "    if settings.get(key):",
                "        base_path = settings[key]",
                "        break",
                "if base_path and not base_path.startswith('/'):",
                "    base_path = '/' + base_path",
                "panel_url = existing_panel_url",
                "if host and port:",
                "    panel_url = f'http://{host}:{port}{base_path}'",
                "emit('PANEL_URL', panel_url)",
                "emit('PANEL_USERNAME', username)",
                "emit('INBOUND_ID', inbound_id)",
                "emit('INBOUND_PORT', inbound_details.get('port', ''))",
                "emit('INBOUND_PROTOCOL', inbound_details.get('protocol', ''))",
                "emit('INBOUND_TRANSPORT', inbound_details.get('transport', ''))",
                "emit('INBOUND_SECURITY', inbound_details.get('security', ''))",
                "emit('LISTENER_STATUS', detect_listener_status(inbound_details.get('port', '')))",
                "PY",
                "echo DROPCATCH_VPN_AUTOCONFIG_END",
            ]
        )
    )


def _build_vpn_create_inbound_command(worker: WorkerNode | None) -> str:
    host = ""
    existing_panel_url = ""
    existing_inbound_id = ""
    if worker is not None:
        host = worker.vpn_public_host or worker.ssh_host or worker.ip_address or ""
        existing_panel_url = worker.vpn_panel_url or ""
        existing_inbound_id = str(worker.vpn_inbound_id or "")
    return _bash(
        "\n".join(
            [
                "set -e",
                f"export DROPCATCH_HOST={_shell_quote(host)}",
                f"export DROPCATCH_EXISTING_PANEL_URL={_shell_quote(existing_panel_url)}",
                f"export DROPCATCH_EXISTING_INBOUND_ID={_shell_quote(existing_inbound_id)}",
                "echo DROPCATCH_VPN_CREATE_INBOUND_BEGIN",
                'printf "DROPCATCH_VPN_PUBLIC_HOST=%s\\n" "$DROPCATCH_HOST"',
                "ACTIVE=$(systemctl is-active x-ui.service 2>/dev/null || systemctl is-active x-ui 2>/dev/null || systemctl is-active 3x-ui.service 2>/dev/null || true)",
                'printf "DROPCATCH_VPN_XUI_ACTIVE=%s\\n" "$ACTIVE"',
                "python3 - <<'PY'",
                "import http.cookiejar, json, os, re, socket, sqlite3, subprocess, time, urllib.error, urllib.parse, urllib.request",
                "",
                "host = os.environ.get('DROPCATCH_HOST', '')",
                "existing_panel_url = os.environ.get('DROPCATCH_EXISTING_PANEL_URL', '')",
                "existing_inbound_id = os.environ.get('DROPCATCH_EXISTING_INBOUND_ID', '')",
                "db_paths = [",
                "    '/etc/x-ui/x-ui.db',",
                "    '/usr/local/x-ui/bin/x-ui.db',",
                "    '/usr/local/x-ui/x-ui.db',",
                "]",
                "",
                "def emit(key, value):",
                "    if value is not None and str(value).strip():",
                "        sanitized = str(value).strip().replace('\\n', ' ')[:1200]",
                "        print(f'DROPCATCH_VPN_{key}={sanitized}')",
                "",
                "def rows_as_dicts(cursor):",
                "    columns = [item[0] for item in cursor.description or []]",
                "    return [dict(zip(columns, row)) for row in cursor.fetchall()]",
                "",
                "def looks_like_sha256_hex(value):",
                "    return bool(re.fullmatch(r'[0-9a-fA-F]{64}', str(value or '').strip()))",
                "",
                "def extract_api_token(output):",
                "    text = str(output or '').replace('\\r', '\\n')",
                "    patterns = [",
                "        r'(?i)api[_ -]?token\\s*[:=]\\s*([A-Za-z0-9._~+/=-]{16,})',",
                "        r'(?i)token\\s*[:=]\\s*([A-Za-z0-9._~+/=-]{16,})',",
                "        r'\\b([A-Za-z0-9._~+/=-]{32,})\\b',",
                "    ]",
                "    for pattern in patterns:",
                "        match = re.search(pattern, text)",
                "        if match:",
                "            token = match.group(1).strip().strip('\\'\\\"')",
                "            if not looks_like_sha256_hex(token):",
                "                return token",
                "    return ''",
                "",
                "def read_cli_api_token():",
                "    commands = [",
                "        ['/usr/local/x-ui/x-ui', 'setting', '-getApiToken', 'true'],",
                "        ['/etc/x-ui/x-ui', 'setting', '-getApiToken', 'true'],",
                "        ['x-ui', 'setting', '-getApiToken', 'true'],",
                "    ]",
                "    last_error = ''",
                "    for command in commands:",
                "        try:",
                "            output = subprocess.check_output(command, stderr=subprocess.STDOUT, text=True, timeout=20)",
                "        except Exception as exc:",
                "            last_error = f'{command[0]}: {exc}'",
                "            continue",
                "        token = extract_api_token(output)",
                "        if token:",
                "            return token, command[0]",
                "        last_error = f'{command[0]}: apiToken not found in output'",
                "    return '', last_error",
                "",
                "def table_columns(conn, table):",
                "    quoted_table = '\"' + table.replace('\"', '\"\"') + '\"'",
                "    return [row[1] for row in conn.execute(f'pragma table_info({quoted_table})')]",
                "",
                "def summarize_inbound_rows(conn, tables):",
                "    notes = []",
                "    for table in ('inbounds', 'client_inbounds', 'inbound_client_ips', 'inbound_fallbacks', 'nodes'):",
                "        if table not in tables:",
                "            continue",
                "        quoted_table = '\"' + table.replace('\"', '\"\"') + '\"'",
                "        columns = table_columns(conn, table)",
                "        parts = []",
                "        try:",
                "            parts.append(f'rows={conn.execute(f\"select count(*) from {quoted_table}\").fetchone()[0]}')",
                "        except Exception as exc:",
                "            parts.append(f'rows_error={exc}')",
                "        for enabled_column in ('enable', 'enabled'):",
                "            if enabled_column in columns:",
                "                quoted_enabled = '\"' + enabled_column.replace('\"', '\"\"') + '\"'",
                "                try:",
                "                    parts.append(f'enabled={conn.execute(f\"select count(*) from {quoted_table} where coalesce({quoted_enabled}, 1) != 0\").fetchone()[0]}')",
                "                except Exception:",
                "                    pass",
                "                break",
                "        if 'id' in columns:",
                "            try:",
                "                ids = [str(row[0]) for row in conn.execute(f'select id from {quoted_table} order by id limit 5').fetchall()]",
                "                if ids:",
                "                    parts.append('ids=' + ','.join(ids))",
                "            except Exception:",
                "                pass",
                "        notes.append(f'{table}:' + ','.join(parts))",
                "    return ';'.join(notes)",
                "",
                "def detect_inbound_id(conn, tables):",
                "    if 'inbounds' not in tables:",
                "        return ''",
                "    columns = table_columns(conn, 'inbounds')",
                "    if 'id' not in columns:",
                "        return ''",
                "    quoted_table = '\"inbounds\"'",
                "    where_parts = ['id is not null']",
                "    for enabled_column in ('enable', 'enabled'):",
                "        if enabled_column in columns:",
                "            quoted_enabled = '\"' + enabled_column.replace('\"', '\"\"') + '\"'",
                "            where_parts.append(f'coalesce({quoted_enabled}, 1) != 0')",
                "            break",
                "    try:",
                "        row = conn.execute(f'select id from {quoted_table} where ' + ' and '.join(where_parts) + ' order by id limit 1').fetchone()",
                "    except Exception:",
                "        return ''",
                "    return str(row[0]).strip() if row and str(row[0]).strip().isdigit() else ''",
                "",
                "def read_db_state():",
                "    state = {'settings': {}, 'username': '', 'password': '', 'api_token': '', 'api_token_source': '', 'inbound_id': '', 'diagnostics': [], 'rows': ''}",
                "    cli_token, cli_source = read_cli_api_token()",
                "    if cli_token:",
                "        state['api_token'] = cli_token",
                "        state['api_token_source'] = 'cli:' + cli_source",
                "        emit('API_TOKEN_STATUS', 'cli_token_detected')",
                "    elif cli_source:",
                "        state['diagnostics'].append('api_token_cli_error=' + cli_source)",
                "        emit('API_TOKEN_STATUS', 'cli_token_unavailable')",
                "    for path in db_paths:",
                "        if not os.path.exists(path):",
                "            continue",
                "        try:",
                "            conn = sqlite3.connect(path)",
                "            try:",
                "                tables = {row[0] for row in conn.execute(\"select name from sqlite_master where type='table'\")}",
                "                state['diagnostics'].append(path + ': tables=' + ','.join(sorted(tables)))",
                "                rows_note = summarize_inbound_rows(conn, tables)",
                "                state['rows'] = rows_note or state['rows']",
                "                if 'settings' in tables and not state['settings']:",
                "                    cur = conn.execute('select * from settings')",
                "                    for row in rows_as_dicts(cur):",
                "                        key = row.get('key') or row.get('name') or row.get('setting') or row.get('item')",
                "                        value = row.get('value') if 'value' in row else row.get('val')",
                "                        if key is not None and value is not None:",
                "                            state['settings'][str(key)] = str(value)",
                "                if 'users' in tables and not state['username']:",
                "                    cur = conn.execute('select * from users order by id limit 1')",
                "                    users = rows_as_dicts(cur)",
                "                    if users:",
                "                        user = users[0]",
                "                        for column in ('username', 'user_name', 'login', 'email'):",
                "                            if user.get(column):",
                "                                state['username'] = str(user[column])",
                "                                break",
                "                        for column in ('password', 'passwd', 'pass'):",
                "                            if user.get(column):",
                "                                state['password'] = str(user[column])",
                "                                break",
                "                if 'api_tokens' in tables and not state['api_token']:",
                "                    token_columns = table_columns(conn, 'api_tokens')",
                "                    token_column = ''",
                "                    for column in ('token', 'access_token', 'api_token', 'key', 'value'):",
                "                        if column in token_columns:",
                "                            token_column = column",
                "                            break",
                "                    if token_column:",
                "                        quoted_token_column = '\"' + token_column.replace('\"', '\"\"') + '\"'",
                "                        try:",
                "                            row = conn.execute(f'select {quoted_token_column} from \"api_tokens\" order by id desc limit 1').fetchone()",
                "                            if row and row[0]:",
                "                                token_value = str(row[0]).strip()",
                "                                if looks_like_sha256_hex(token_value):",
                "                                    state['diagnostics'].append('api_token_db_hash_detected=true')",
                "                                else:",
                "                                    state['api_token'] = token_value",
                "                                    state['api_token_source'] = 'db:' + token_column",
                "                        except Exception as exc:",
                "                            emit('INBOUND_CREATE_ERROR', f'Cannot read 3x-UI API token: {exc}')",
                "                state['inbound_id'] = state['inbound_id'] or detect_inbound_id(conn, tables)",
                "            finally:",
                "                conn.close()",
                "        except Exception as exc:",
                "            emit('AUTOCONFIG_DB_ERROR', f'{path}: {exc}')",
                "    return state",
                "",
                "def choose_port():",
                "    for port in (443, 8443, 2053, 2083, 2096, 30000, 30001, 30002):",
                "        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:",
                "            sock.settimeout(0.2)",
                "            if sock.connect_ex(('127.0.0.1', port)) != 0:",
                "                return port",
                "    return 30003",
                "",
                "def build_panel_url(settings):",
                "    port = ''",
                "    for key in ('webPort', 'web_port', 'port', 'panel_port'):",
                "        if settings.get(key):",
                "            port = settings[key]",
                "            break",
                "    base_path = ''",
                "    for key in ('webBasePath', 'web_base_path', 'base_path', 'webPath'):",
                "        if settings.get(key):",
                "            base_path = settings[key]",
                "            break",
                "    if base_path and not base_path.startswith('/'):",
                "        base_path = '/' + base_path",
                "    panel_url = existing_panel_url",
                "    local_url = ''",
                "    if port:",
                "        local_url = f'http://127.0.0.1:{port}{base_path}'",
                "        if host:",
                "            panel_url = f'http://{host}:{port}{base_path}'",
                "    return panel_url, local_url",
                "",
                "def request_json(opener, url, payload=None, *, api_token='', method=None):",
                "    headers = {",
                "        'Accept': 'application/json',",
                "        'Content-Type': 'application/json',",
                "        'User-Agent': 'Mozilla/5.0',",
                "        'X-Requested-With': 'XMLHttpRequest',",
                "    }",
                "    parsed = urllib.parse.urlsplit(url)",
                "    if parsed.scheme and parsed.netloc:",
                "        headers['Origin'] = f'{parsed.scheme}://{parsed.netloc}'",
                "        headers['Referer'] = f'{parsed.scheme}://{parsed.netloc}/'",
                "    if api_token:",
                "        headers['Authorization'] = 'Bearer ' + api_token",
                "    data = None if payload is None else json.dumps(payload).encode()",
                "    req = urllib.request.Request(url, data=data, headers=headers, method=method)",
                "    return opener.open(req, timeout=10)",
                "",
                "def request_form(opener, url, payload):",
                "    encoded = urllib.parse.urlencode(payload).encode()",
                "    parsed = urllib.parse.urlsplit(url)",
                "    headers = {",
                "        'Accept': 'application/json',",
                "        'Content-Type': 'application/x-www-form-urlencoded',",
                "        'User-Agent': 'Mozilla/5.0',",
                "        'X-Requested-With': 'XMLHttpRequest',",
                "    }",
                "    if parsed.scheme and parsed.netloc:",
                "        headers['Origin'] = f'{parsed.scheme}://{parsed.netloc}'",
                "        headers['Referer'] = f'{parsed.scheme}://{parsed.netloc}/'",
                "    req = urllib.request.Request(url, data=encoded, headers=headers)",
                "    return opener.open(req, timeout=10)",
                "",
                "def read_response(response, limit=600):",
                "    return response.read().decode('utf-8', errors='replace')[:limit]",
                "",
                "def create_inbound_with_session(opener, local_url, payload):",
                "    login_payload = {'username': state['username'], 'password': state['password']}",
                "    login_error = ''",
                "    for login_mode in ('form', 'json'):",
                "        try:",
                "            if login_mode == 'form':",
                "                login_response = request_form(opener, local_url.rstrip('/') + '/login', login_payload)",
                "            else:",
                "                login_response = request_json(opener, local_url.rstrip('/') + '/login', login_payload)",
                "            emit('INBOUND_CREATE_STATUS', f'login_{login_mode}_http_{login_response.status}: {read_response(login_response)}')",
                "            list_response = request_json(opener, local_url.rstrip('/') + '/panel/api/inbounds/list', None, method='GET')",
                "            emit('INBOUND_CREATE_STATUS', f'list_with_session_http_{list_response.status}: {read_response(list_response)}')",
                "            return request_json(opener, local_url.rstrip('/') + '/panel/api/inbounds/add', payload)",
                "        except urllib.error.HTTPError as exc:",
                "            body = exc.read().decode('utf-8', errors='replace')[:600]",
                "            login_error = f'{login_mode}: HTTP {exc.code}: {body}'",
                "            emit('INBOUND_CREATE_STATUS', f'login_{login_mode}_failed: {login_error}')",
                "            if exc.code not in (400, 401, 403, 404, 405):",
                "                raise",
                "    raise RuntimeError('session login failed; ' + login_error)",
                "",
                "state = read_db_state()",
                "inbound_id = existing_inbound_id or state['inbound_id']",
                "emit('API_TOKEN_SOURCE', state.get('api_token_source') or ('none' if not state.get('api_token') else 'unknown'))",
                "emit('DB_DIAGNOSTIC', ' | '.join(state['diagnostics']))",
                "emit('INBOUND_ROWS', state['rows'])",
                "panel_url, local_url = build_panel_url(state['settings'])",
                "emit('PANEL_URL', panel_url)",
                "emit('PANEL_USERNAME', state['username'])",
                "if inbound_id:",
                "    emit('INBOUND_CREATE_STATUS', 'existing')",
                "    emit('INBOUND_ID', inbound_id)",
                "else:",
                "    if not local_url or (not state['api_token'] and (not state['username'] or not state['password'])):",
                "        emit('INBOUND_CREATE_ERROR', 'Cannot create inbound: panel URL or local 3x-UI credentials were not found in x-ui database')",
                "    else:",
                "        inbound_port = choose_port()",
                "        payload = {",
                "            'up': 0,",
                "            'down': 0,",
                "            'total': 0,",
                "            'remark': 'dropcatch-vpn',",
                "            'enable': True,",
                "            'expiryTime': 0,",
                "            'listen': '',",
                "            'port': inbound_port,",
                "            'protocol': 'vless',",
                "            'settings': json.dumps({'clients': [], 'decryption': 'none', 'fallbacks': []}),",
                "            'streamSettings': json.dumps({'network': 'tcp', 'security': 'none', 'tcpSettings': {'acceptProxyProtocol': False, 'header': {'type': 'none'}}}),",
                "            'sniffing': json.dumps({'enabled': True, 'destOverride': ['http', 'tls', 'quic'], 'metadataOnly': False, 'routeOnly': False}),",
                "        }",
                "        cookie_jar = http.cookiejar.CookieJar()",
                "        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))",
                "        try:",
                "            current_step = 'prepare'",
                "            auth_mode = 'api_token' if state['api_token'] else 'session'",
                "            emit('INBOUND_CREATE_AUTH', auth_mode)",
                "            if state['api_token']:",
                "                try:",
                "                    current_step = 'list_with_api_token'",
                "                    list_response = request_json(opener, local_url.rstrip('/') + '/panel/api/inbounds/list', None, api_token=state['api_token'], method='GET')",
                "                    emit('INBOUND_CREATE_STATUS', f'list_with_api_token_http_{list_response.status}: {read_response(list_response)}')",
                "                    current_step = 'add_with_api_token'",
                "                    add_response = request_json(opener, local_url.rstrip('/') + '/panel/api/inbounds/add', payload, api_token=state['api_token'])",
                "                except urllib.error.HTTPError as exc:",
                "                    body = exc.read().decode('utf-8', errors='replace')[:600]",
                "                    emit('INBOUND_CREATE_STATUS', f'{current_step}_failed: HTTP {exc.code}: {body}')",
                "                    if exc.code not in (401, 403) or not state['username'] or not state['password']:",
                "                        raise",
                "                    emit('INBOUND_CREATE_AUTH', 'session_fallback')",
                "                    current_step = 'add_with_session_fallback'",
                "                    add_response = create_inbound_with_session(opener, local_url, payload)",
                "            else:",
                "                current_step = 'add_with_session'",
                "                add_response = create_inbound_with_session(opener, local_url, payload)",
                "            body = read_response(add_response)",
                "            emit('INBOUND_CREATE_STATUS', f'api_http_{add_response.status}: {body}')",
                "            time.sleep(1)",
                "            state = read_db_state()",
                "            emit('INBOUND_ROWS', state['rows'])",
                "            inbound_id = state['inbound_id']",
                "            if inbound_id:",
                "                emit('INBOUND_ID', inbound_id)",
                "            else:",
                "                emit('INBOUND_CREATE_ERROR', f'3x-UI API returned HTTP {add_response.status}, but inbound ID was not detected after create; body={body}')",
                "        except urllib.error.HTTPError as exc:",
                "            body = exc.read().decode('utf-8', errors='replace')[:600]",
                "            emit('INBOUND_CREATE_ERROR', f'{current_step}: HTTP {exc.code}: {body}')",
                "        except Exception as exc:",
                "            emit('INBOUND_CREATE_ERROR', str(exc))",
                "PY",
                "systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service || true",
                "echo DROPCATCH_VPN_CREATE_INBOUND_END",
            ]
        )
    )


def parse_vpn_autoconfig_output(log: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    prefix = "DROPCATCH_VPN_"
    for raw_line in log.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.removeprefix(prefix).lower()
        if normalized_key in VPN_AUTOCONFIG_KEYS and value.strip():
            metadata[normalized_key] = value.strip()
    return metadata


def apply_vpn_autoconfig_metadata(worker: WorkerNode, metadata: Mapping[str, str]) -> None:
    public_host = metadata.get("public_host")
    panel_url = metadata.get("panel_url")
    panel_username = metadata.get("panel_username")
    inbound_id = metadata.get("inbound_id")
    inbound_port = metadata.get("inbound_port")

    if public_host:
        worker.vpn_public_host = public_host
    if panel_url:
        worker.vpn_panel_url = panel_url
    if panel_username:
        worker.vpn_panel_username = panel_username
    if inbound_id and inbound_id.isdigit():
        worker.vpn_inbound_id = int(inbound_id)
    if inbound_port and inbound_port.isdigit():
        worker.vpn_inbound_port = int(inbound_port)
    for attr, key in (
        ("vpn_inbound_protocol", "inbound_protocol"),
        ("vpn_inbound_transport", "inbound_transport"),
        ("vpn_inbound_security", "inbound_security"),
        ("vpn_listener_status", "listener_status"),
    ):
        value = metadata.get(key)
        if value:
            setattr(worker, attr, value[:32])

    active_state = (metadata.get("xui_active") or "").lower()
    worker.vpn_last_checked_at = utcnow()
    if active_state in {"active", "running"} and worker.vpn_panel_url and worker.vpn_inbound_id:
        if worker.vpn_listener_status == "not_listening" and worker.vpn_inbound_port:
            worker.vpn_runtime_status = "needs_config"
            worker.vpn_last_error = (
                f"3x-UI inbound #{worker.vpn_inbound_id} detected, but VPN port "
                f"{worker.vpn_inbound_port} is not listening. Restart 3x-UI or recreate inbound."
            )
        else:
            worker.vpn_runtime_status = "ready"
            worker.vpn_last_error = None
        return
    if active_state not in {"active", "running"}:
        worker.vpn_runtime_status = "error"
        worker.vpn_last_error = "3x-UI service is not active or was not detected"
        return
    worker.vpn_runtime_status = "needs_config"
    missing_parts = []
    if not worker.vpn_panel_url:
        missing_parts.append("panel URL")
    if not worker.vpn_inbound_id:
        missing_parts.append("inbound ID")
    diagnostic_bits = []
    if metadata.get("inbound_candidates"):
        diagnostic_bits.append(f"inbound candidates: {metadata['inbound_candidates'][:500]}")
    if metadata.get("inbound_rows"):
        diagnostic_bits.append(f"inbound rows: {metadata['inbound_rows'][:500]}")
    if metadata.get("api_token_source"):
        diagnostic_bits.append(f"api token source: {metadata['api_token_source'][:160]}")
    if metadata.get("api_token_status"):
        diagnostic_bits.append(f"api token status: {metadata['api_token_status'][:300]}")
    if metadata.get("inbound_create_auth"):
        diagnostic_bits.append(f"inbound create auth: {metadata['inbound_create_auth'][:120]}")
    if metadata.get("inbound_create_status"):
        diagnostic_bits.append(f"inbound create status: {metadata['inbound_create_status'][:500]}")
    if metadata.get("inbound_create_error"):
        diagnostic_bits.append(f"inbound create error: {metadata['inbound_create_error'][:500]}")
    if metadata.get("db_diagnostic"):
        diagnostic_bits.append(f"db: {metadata['db_diagnostic'][:500]}")
    if metadata.get("autoconfig_db_error"):
        diagnostic_bits.append(f"db error: {metadata['autoconfig_db_error'][:500]}")
    suffix = f"; {'; '.join(diagnostic_bits)}" if diagnostic_bits else ""
    missing_text = ", ".join(missing_parts) or "required settings"
    worker.vpn_last_error = (
        f"3x-UI is installed, but auto-config did not detect {missing_text}. "
        "If inbound ID is missing, create a VPN inbound from the control panel with 'Create inbound' "
        f"or create it in 3x-UI, then run auto-config again{suffix}"
    )


def build_worker_maintenance_commands(
    action: str,
    *,
    worker: WorkerNode | None = None,
    discovery_settings: DiscoveryRuntimeSettings | None = None,
) -> list[str]:
    if action == "check":
        return [
            "hostname",
            "whoami",
            "systemctl is-active domain-drop-worker.service || true",
        ]
    if action == "install":
        if worker is None:
            raise ValueError("Worker is required for install action")
        settings = get_settings()
        runtime_base_url = settings.worker_runtime_public_base_url or "http://CONTROL_SERVER_IP:8080"
        env_command = _build_printf_command(
            "/opt/domain-drop-catcher/worker/.env",
            _build_worker_env_lines(
                worker,
                runtime_base_url=runtime_base_url,
                simulate_mode=False,
                discovery_settings=discovery_settings,
            ),
        )
        return [
            "apt-get update",
            "apt-get install -y git python3.11 python3.11-venv python3.11-dev build-essential",
            "test ! -e /opt/domain-drop-catcher",
            f"git clone {_shell_quote(settings.worker_setup_repository_url)} /opt/domain-drop-catcher",
            f"{_shell_quote(settings.worker_setup_python_bin)} -m venv /opt/domain-drop-catcher/worker/.venv",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install --upgrade pip",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
            env_command,
            _build_worker_service_command(),
            "systemctl daemon-reload",
            "systemctl enable --now domain-drop-worker.service",
            "systemctl is-active domain-drop-worker.service",
        ]
    if action == "update":
        discovery_env_commands = _build_worker_discovery_env_commands(discovery_settings)
        return [
            "cd /opt/domain-drop-catcher && git pull --ff-only origin main",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
            *discovery_env_commands,
            "systemctl daemon-reload",
            "systemctl restart domain-drop-worker.service",
            "systemctl is-active domain-drop-worker.service",
        ]
    if action == "vpn_check":
        return [
            "hostname",
            "whoami",
            "command -v x-ui || true",
            _build_vpn_status_command(),
            _build_vpn_ready_command(),
            _build_vpn_autoconfig_command(worker),
            _bash("systemctl --no-pager --full status x-ui.service || systemctl --no-pager --full status x-ui || true"),
            _bash("ss -lntp | grep -E ':(443|8443|2053|54321|62789)\\b' || true"),
            "test -d /usr/local/x-ui && echo x-ui-dir-present || true",
        ]
    if action == "vpn_install":
        return [
            "apt-get update",
            "apt-get install -y curl socat jq tar",
            _bash(
                "set -e; "
                "if command -v x-ui >/dev/null 2>&1 || systemctl list-unit-files | grep -Eq '^x-ui(\\.service)?'; "
                "then echo '3x-ui already installed'; "
                "else curl -fsSL https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh -o /tmp/3x-ui-install.sh "
                "&& chmod +x /tmp/3x-ui-install.sh "
                "&& yes '' | timeout 600 bash /tmp/3x-ui-install.sh; "
                "fi"
            ),
            "systemctl enable --now x-ui.service || systemctl enable --now x-ui || systemctl enable --now 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    if action == "vpn_update":
        return [
            "apt-get install -y curl || true",
            _bash("if command -v x-ui >/dev/null 2>&1; then x-ui update; else echo 'x-ui command not found'; fi"),
            "systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    if action == "vpn_restart":
        return [
            "systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    if action == "vpn_autoconfig":
        return [
            "hostname",
            "whoami",
            "command -v sqlite3 || true",
            _build_vpn_autoconfig_command(worker),
        ]
    if action == "vpn_create_inbound":
        return [
            "hostname",
            "whoami",
            "command -v python3",
            _build_vpn_create_inbound_command(worker),
            _build_vpn_autoconfig_command(worker),
        ]
    raise ValueError(f"Unsupported worker maintenance action: {action}")


async def run_worker_maintenance_job(job_id: int) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        if job is None:
            return
        worker = await session.get(WorkerNode, job.worker_id)
        if worker is None:
            job.status = "failed"
            job.error_message = "Worker not found"
            job.finished_at = utcnow()
            await session.commit()
            return

        job.status = "running"
        job.started_at = utcnow()
        job.updated_at = utcnow()
        if job.action in VPN_MAINTENANCE_ACTIONS:
            worker.vpn_runtime_status = VPN_RUNNING_STATUS_BY_ACTION[job.action]
            worker.vpn_last_error = None
            worker.vpn_last_checked_at = utcnow()
        await session.commit()

        try:
            discovery_settings = await get_discovery_runtime_settings(session, get_settings())
            commands = build_worker_maintenance_commands(
                job.action,
                worker=worker,
                discovery_settings=discovery_settings,
            )
            log = await execute_worker_ssh_commands(worker, commands)
        except Exception as exc:  # pragma: no cover - exact SSH errors depend on environment
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            if job.action in VPN_MAINTENANCE_ACTIONS:
                worker.vpn_runtime_status = "error"
                worker.vpn_last_error = str(exc)
                worker.vpn_last_checked_at = utcnow()
                session.add(
                    VpnNodeEvent(
                        worker_id=worker.id,
                        level="error",
                        event_type=job.action,
                        message=f"VPN maintenance failed: {str(exc)[:500]}",
                        details={"job_id": job.id},
                    )
                )
            else:
                worker.ssh_last_check_status = "failed"
                worker.ssh_last_check_message = str(exc)
                worker.ssh_last_checked_at = utcnow()
            await session.commit()
            return

        job.status = "succeeded"
        job.log = log
        job.finished_at = utcnow()
        job.updated_at = utcnow()
        if job.action == "check":
            worker.ssh_last_check_status = "ok"
            worker.ssh_last_check_message = "SSH check succeeded"
            worker.ssh_last_checked_at = utcnow()
        if job.action in {"vpn_autoconfig", "vpn_create_inbound", "vpn_check"}:
            metadata = parse_vpn_autoconfig_output(log)
            apply_vpn_autoconfig_metadata(worker, metadata)
            session.add(
                VpnNodeEvent(
                    worker_id=worker.id,
                    level="info" if worker.vpn_runtime_status == "ready" else "warning",
                    event_type=job.action,
                    message=(
                        "VPN config finished: "
                        f"status={worker.vpn_runtime_status} "
                        f"panel={'yes' if worker.vpn_panel_url else 'no'} "
                        f"inbound={'yes' if worker.vpn_inbound_id else 'no'}"
                    ),
                    details={
                        "job_id": job.id,
                        "detected_keys": sorted(metadata.keys()),
                        "inbound_candidates": metadata.get("inbound_candidates"),
                        "inbound_rows": metadata.get("inbound_rows"),
                        "inbound_create_status": metadata.get("inbound_create_status"),
                        "inbound_create_error": metadata.get("inbound_create_error"),
                        "db_diagnostic": metadata.get("db_diagnostic"),
                    },
                )
            )
        elif job.action in VPN_MAINTENANCE_ACTIONS:
            worker.vpn_runtime_status = "ready"
            worker.vpn_last_error = None
            worker.vpn_last_checked_at = utcnow()
            session.add(
                VpnNodeEvent(
                    worker_id=worker.id,
                    level="info",
                    event_type=job.action,
                    message="VPN maintenance succeeded",
                    details={"job_id": job.id},
                )
            )
        await session.commit()


async def execute_worker_ssh_commands(worker: WorkerNode, commands: list[str]) -> str:
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - dependency is installed in production
        raise RuntimeError("asyncssh is not installed; run backend pip install after updating the project") from exc

    host = worker.ssh_host or worker.ip_address
    if not host:
        raise ValueError("Worker SSH host is missing")
    if not worker.ssh_username:
        raise ValueError("Worker SSH username is missing")
    if not worker.ssh_password and not worker.ssh_key_path:
        raise ValueError("Worker SSH password or key path is missing")

    connect_kwargs = {
        "host": host,
        "port": worker.ssh_port or 22,
        "username": worker.ssh_username,
        "known_hosts": None,
    }
    if worker.ssh_password:
        connect_kwargs["password"] = worker.ssh_password
    if worker.ssh_key_path:
        connect_kwargs["client_keys"] = [worker.ssh_key_path]

    log_parts: list[str] = []
    async with asyncssh.connect(**connect_kwargs) as connection:
        for command in commands:
            log_parts.append(f"$ {command}")
            result = await connection.run(command, check=False)
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            if stdout:
                log_parts.append(stdout)
            if stderr:
                log_parts.append(stderr)
            log_parts.append(f"exit={result.exit_status}")
            if result.exit_status != 0:
                raise RuntimeError(f"Command failed with exit={result.exit_status}: {command}\n{stderr or stdout}")
    return "\n".join(log_parts)
