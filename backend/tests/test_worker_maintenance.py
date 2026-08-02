from app.db.models import WorkerNode
from app.services.worker_maintenance import (
    apply_vpn_autoconfig_metadata,
    build_worker_maintenance_commands,
    parse_vpn_autoconfig_output,
)


def test_build_vpn_autoconfig_commands_include_detection_markers():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    commands = build_worker_maintenance_commands("vpn_autoconfig", worker=worker)

    combined = "\n".join(commands)
    assert "DROPCATCH_VPN_AUTOCONFIG_BEGIN" in combined
    assert "DROPCATCH_VPN_PUBLIC_HOST" in combined
    assert "DB_DIAGNOSTIC" in combined
    assert "INBOUND_CANDIDATES" in combined
    assert "INBOUND_ROWS" in combined
    assert "INBOUND_ID" in combined
    assert "INBOUND_PORT" in combined
    assert "LISTENER_STATUS" in combined
    assert "detect_listener_status" in combined


def test_build_vpn_check_commands_refresh_endpoint_metadata():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    commands = build_worker_maintenance_commands("vpn_check", worker=worker)

    combined = "\n".join(commands)
    assert "DROPCATCH_VPN_AUTOCONFIG_BEGIN" in combined
    assert "LISTENER_STATUS" in combined


def test_build_vpn_create_inbound_commands_use_3xui_api():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    commands = build_worker_maintenance_commands("vpn_create_inbound", worker=worker)

    combined = "\n".join(commands)
    assert "DROPCATCH_VPN_CREATE_INBOUND_BEGIN" in combined
    assert "getApiToken" in combined
    assert "extract_api_token" in combined
    assert "API_TOKEN_SOURCE" in combined
    assert "api_token_db_hash_detected" in combined
    assert "api_tokens" in combined
    assert "Authorization" in combined
    assert "session_fallback" in combined
    assert "request_form" in combined
    assert "/panel/api/inbounds/list" in combined
    assert "/panel/api/inbounds/add" in combined
    assert "INBOUND_CREATE_AUTH" in combined
    assert "INBOUND_CREATE_STATUS" in combined
    assert "INBOUND_CREATE_ERROR" in combined
    assert "DROPCATCH_VPN_AUTOCONFIG_BEGIN" in combined
    assert "insert into inbounds" not in combined.lower()


def test_parse_vpn_autoconfig_output_reads_safe_fields_only():
    metadata = parse_vpn_autoconfig_output(
        "\n".join(
            [
                "DROPCATCH_VPN_PUBLIC_HOST=2.27.20.255",
                "DROPCATCH_VPN_PANEL_URL=http://2.27.20.255:2053/panel/",
                "DROPCATCH_VPN_PANEL_USERNAME=admin",
                "DROPCATCH_VPN_PANEL_PASSWORD=secret",
                "DROPCATCH_VPN_INBOUND_ID=7",
                "DROPCATCH_VPN_INBOUND_PORT=443",
                "DROPCATCH_VPN_INBOUND_PROTOCOL=vless",
                "DROPCATCH_VPN_INBOUND_TRANSPORT=tcp",
                "DROPCATCH_VPN_INBOUND_SECURITY=reality",
                "DROPCATCH_VPN_LISTENER_STATUS=listening",
                "DROPCATCH_VPN_XUI_ACTIVE=active",
                "DROPCATCH_VPN_DB_DIAGNOSTIC=/etc/x-ui/x-ui.db: tables=settings,inbounds",
                "DROPCATCH_VPN_INBOUND_CANDIDATES=inbounds.id",
                "DROPCATCH_VPN_INBOUND_ROWS=inbounds:rows=1,enabled=1,ids=7",
                "DROPCATCH_VPN_API_TOKEN_SOURCE=cli:/usr/local/x-ui/x-ui",
                "DROPCATCH_VPN_API_TOKEN_STATUS=cli_token_detected",
                "DROPCATCH_VPN_INBOUND_CREATE_AUTH=api_token",
                "DROPCATCH_VPN_INBOUND_CREATE_STATUS=created",
                "DROPCATCH_VPN_INBOUND_CREATE_ERROR=will be kept if API fails",
            ]
        )
    )

    assert metadata == {
        "public_host": "2.27.20.255",
        "panel_url": "http://2.27.20.255:2053/panel/",
        "panel_username": "admin",
        "inbound_id": "7",
        "inbound_port": "443",
        "inbound_protocol": "vless",
        "inbound_transport": "tcp",
        "inbound_security": "reality",
        "listener_status": "listening",
        "xui_active": "active",
        "db_diagnostic": "/etc/x-ui/x-ui.db: tables=settings,inbounds",
        "inbound_candidates": "inbounds.id",
        "inbound_rows": "inbounds:rows=1,enabled=1,ids=7",
        "api_token_source": "cli:/usr/local/x-ui/x-ui",
        "api_token_status": "cli_token_detected",
        "inbound_create_auth": "api_token",
        "inbound_create_status": "created",
        "inbound_create_error": "will be kept if API fails",
    }


def test_apply_vpn_autoconfig_metadata_marks_ready_without_touching_password():
    worker = WorkerNode(
        name="vpn-1",
        ip_address="2.27.20.255",
        vpn_panel_password="existing-password",
    )

    apply_vpn_autoconfig_metadata(
        worker,
        {
            "public_host": "2.27.20.255",
            "panel_url": "http://2.27.20.255:2053/panel/",
            "panel_username": "admin",
            "inbound_id": "7",
            "inbound_port": "443",
            "inbound_protocol": "vless",
            "inbound_transport": "tcp",
            "inbound_security": "reality",
            "listener_status": "listening",
            "xui_active": "active",
        },
    )

    assert worker.vpn_public_host == "2.27.20.255"
    assert worker.vpn_panel_url == "http://2.27.20.255:2053/panel/"
    assert worker.vpn_panel_username == "admin"
    assert worker.vpn_panel_password == "existing-password"
    assert worker.vpn_inbound_id == 7
    assert worker.vpn_inbound_port == 443
    assert worker.vpn_inbound_protocol == "vless"
    assert worker.vpn_inbound_transport == "tcp"
    assert worker.vpn_inbound_security == "reality"
    assert worker.vpn_listener_status == "listening"
    assert worker.vpn_runtime_status == "ready"
    assert worker.vpn_last_error is None


def test_apply_vpn_autoconfig_metadata_marks_needs_config_when_port_is_closed():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    apply_vpn_autoconfig_metadata(
        worker,
        {
            "public_host": "2.27.20.255",
            "panel_url": "http://2.27.20.255:2053/panel/",
            "inbound_id": "7",
            "inbound_port": "443",
            "listener_status": "not_listening",
            "xui_active": "active",
        },
    )

    assert worker.vpn_runtime_status == "needs_config"
    assert "443" in (worker.vpn_last_error or "")
    assert "not listening" in (worker.vpn_last_error or "")


def test_apply_vpn_autoconfig_metadata_explains_missing_inbound():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    apply_vpn_autoconfig_metadata(
        worker,
        {
            "public_host": "2.27.20.255",
            "panel_url": "http://2.27.20.255:2053/panel/",
            "xui_active": "active",
            "db_diagnostic": "/etc/x-ui/x-ui.db: tables=settings,users",
            "inbound_rows": "inbounds:rows=0,enabled=0",
        },
    )

    assert worker.vpn_runtime_status == "needs_config"
    assert worker.vpn_panel_url == "http://2.27.20.255:2053/panel/"
    assert "inbound ID" in (worker.vpn_last_error or "")
    assert "rows=0" in (worker.vpn_last_error or "")
    assert "settings,users" in (worker.vpn_last_error or "")


def test_apply_vpn_autoconfig_metadata_explains_create_error():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    apply_vpn_autoconfig_metadata(
        worker,
        {
            "public_host": "2.27.20.255",
            "panel_url": "http://2.27.20.255:2053/panel/",
            "xui_active": "active",
            "inbound_rows": "inbounds:rows=0,enabled=0",
            "inbound_create_error": "HTTP 404: API route not found",
        },
    )

    assert worker.vpn_runtime_status == "needs_config"
    assert "HTTP 404" in (worker.vpn_last_error or "")
