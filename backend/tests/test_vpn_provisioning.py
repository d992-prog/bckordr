from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.db.models import VpnAccessKey, VpnNodeEvent, VpnSubscription, WorkerNode
from app.services.vpn_provisioning import (
    VpnClientProvisionPayload,
    build_vpn_client_email,
    build_vpn_client_provision_command,
    ensure_vpn_client_uuid,
    provision_vpn_access_key,
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


def test_ensure_vpn_client_uuid_replaces_legacy_non_uuid_value() -> None:
    access_key = VpnAccessKey(external_uuid="vpn-client-uuid")

    client_uuid = ensure_vpn_client_uuid(access_key)

    assert UUID(access_key.external_uuid or "") == client_uuid
    assert access_key.external_uuid != "vpn-client-uuid"


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


@pytest.mark.asyncio
async def test_provision_vpn_access_key_records_failure_event_without_crashing(monkeypatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def flush(self) -> None:
            return None

        def add(self, item: object) -> None:
            self.added.append(item)

    async def fail_ssh(*args, **kwargs) -> str:
        raise RuntimeError("ssh failed")

    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", fail_ssh)
    subscription = VpnSubscription(
        customer_id=1,
        plan_id=None,
        status="active",
        starts_at=datetime(2026, 8, 1, tzinfo=UTC),
        expires_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    worker = WorkerNode(
        id=7,
        name="vpn-node",
        ip_address="31.77.157.65",
        ssh_host="31.77.157.65",
        ssh_password="secret",
        vpn_inbound_id=1,
    )
    access_key = VpnAccessKey(
        id=12,
        subscription_id=1,
        worker_id=7,
        protocol="vless",
        public_name="phone",
        external_uuid="11111111-1111-1111-1111-111111111111",
    )

    session = FakeSession()
    await provision_vpn_access_key(session, access_key, subscription=subscription, worker=worker)  # type: ignore[arg-type]

    assert access_key.status == "pending_sync"
    assert access_key.last_error == "ssh failed"
    event = next(item for item in session.added if isinstance(item, VpnNodeEvent))
    assert event.event_type == "client_provision_failed"
    assert event.details == {"access_key_id": 12}
