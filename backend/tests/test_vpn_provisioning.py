from datetime import UTC, datetime
from uuid import UUID

from app.db.models import WorkerNode
from app.services.vpn_provisioning import (
    VpnClientProvisionPayload,
    build_vpn_client_email,
    build_vpn_client_provision_command,
    parse_vpn_client_provision_output,
)


def test_parse_vpn_client_provision_output_extracts_markers() -> None:
    log = """
    random line
    DROPCATCH_VPN_CLIENT_STATUS=provisioned
    DROPCATCH_VPN_CLIENT_ID=11111111-1111-1111-1111-111111111111
    DROPCATCH_VPN_CLIENT_EMAIL=dropcatch-12-test-user
    DROPCATCH_VPN_CLIENT_URL=vless://11111111-1111-1111-1111-111111111111@example.com:443?type=tcp#dropcatch-12-test-user
    """

    metadata = parse_vpn_client_provision_output(log)

    assert metadata == {
        "status": "provisioned",
        "id": "11111111-1111-1111-1111-111111111111",
        "email": "dropcatch-12-test-user",
        "url": "vless://11111111-1111-1111-1111-111111111111@example.com:443?type=tcp#dropcatch-12-test-user",
    }


def test_build_vpn_client_email_is_stable_and_safe() -> None:
    assert build_vpn_client_email(12, "Test User! Иван") == "dropcatch-12-test-user"
    assert build_vpn_client_email(12, None) == "dropcatch-12-client"


def test_build_vpn_client_provision_command_contains_payload_and_markers() -> None:
    worker = WorkerNode(
        id=3,
        name="vpn-node",
        ip_address="31.77.157.65",
        vpn_public_host="31.77.157.65",
        vpn_panel_url="http://31.77.157.65:49296/secret/",
        vpn_panel_username="admin",
        vpn_panel_password="pass",
        vpn_inbound_id=1,
    )
    payload = VpnClientProvisionPayload(
        client_uuid=UUID("11111111-1111-1111-1111-111111111111"),
        client_email="dropcatch-12-test-user",
        inbound_id=1,
        protocol="vless",
        expires_at=datetime(2026, 8, 3, tzinfo=UTC),
        max_devices=1,
    )

    command = build_vpn_client_provision_command(worker, payload)

    assert "/panel/api/inbounds/addClient" in command
    assert "DROPCATCH_VPN_CLIENT_URL" in command
    assert "11111111-1111-1111-1111-111111111111" in command
    assert "dropcatch-12-test-user" in command
