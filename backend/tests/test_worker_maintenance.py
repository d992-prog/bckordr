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
    assert "INBOUND_ID" in combined


def test_parse_vpn_autoconfig_output_reads_safe_fields_only():
    metadata = parse_vpn_autoconfig_output(
        "\n".join(
            [
                "DROPCATCH_VPN_PUBLIC_HOST=2.27.20.255",
                "DROPCATCH_VPN_PANEL_URL=http://2.27.20.255:2053/panel/",
                "DROPCATCH_VPN_PANEL_USERNAME=admin",
                "DROPCATCH_VPN_PANEL_PASSWORD=secret",
                "DROPCATCH_VPN_INBOUND_ID=7",
                "DROPCATCH_VPN_XUI_ACTIVE=active",
                "DROPCATCH_VPN_DB_DIAGNOSTIC=/etc/x-ui/x-ui.db: tables=settings,inbounds",
                "DROPCATCH_VPN_INBOUND_CANDIDATES=inbounds.id",
            ]
        )
    )

    assert metadata == {
        "public_host": "2.27.20.255",
        "panel_url": "http://2.27.20.255:2053/panel/",
        "panel_username": "admin",
        "inbound_id": "7",
        "xui_active": "active",
        "db_diagnostic": "/etc/x-ui/x-ui.db: tables=settings,inbounds",
        "inbound_candidates": "inbounds.id",
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
            "xui_active": "active",
        },
    )

    assert worker.vpn_public_host == "2.27.20.255"
    assert worker.vpn_panel_url == "http://2.27.20.255:2053/panel/"
    assert worker.vpn_panel_username == "admin"
    assert worker.vpn_panel_password == "existing-password"
    assert worker.vpn_inbound_id == 7
    assert worker.vpn_runtime_status == "ready"
    assert worker.vpn_last_error is None


def test_apply_vpn_autoconfig_metadata_explains_missing_inbound():
    worker = WorkerNode(name="vpn-1", ip_address="2.27.20.255")

    apply_vpn_autoconfig_metadata(
        worker,
        {
            "public_host": "2.27.20.255",
            "panel_url": "http://2.27.20.255:2053/panel/",
            "xui_active": "active",
            "db_diagnostic": "/etc/x-ui/x-ui.db: tables=settings,users",
        },
    )

    assert worker.vpn_runtime_status == "needs_config"
    assert worker.vpn_panel_url == "http://2.27.20.255:2053/panel/"
    assert "inbound ID" in (worker.vpn_last_error or "")
    assert "settings,users" in (worker.vpn_last_error or "")
