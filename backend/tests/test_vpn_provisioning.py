from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.db.models import VpnAccessKey, VpnNodeEvent, VpnSubscription, WorkerNode
from app.services import vpn_provisioning
from app.services.vpn_provisioning import (
    VpnClientProvisionPayload,
    build_vpn_client_email,
    build_vpn_client_provision_command,
    build_vpn_client_revoke_command,
    ensure_vpn_client_uuid,
    provision_vpn_access_key,
    parse_vpn_client_provision_output,
    revoke_vpn_access_key,
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
    assert '"security": security' in command
    assert 'params["encryption"] = "none"' in command
    assert 'params["headerType"] = str(tcp_header.get("type") or "none").lower()' in command
    assert "client_inbounds" in command
    assert "client_traffics" in command
    assert '"password", "passwd"' in command
    assert 'values["client_id"] = client_pk' in command
    assert "values[column] = numeric_column_value(info.get(column, {}).get(\"type\"))" in command


def test_build_vpn_client_revoke_command_removes_normalized_rows() -> None:
    worker = WorkerNode(id=3, name="vpn-node", ip_address="31.77.157.65", vpn_inbound_id=1)
    access_key = VpnAccessKey(
        id=12,
        subscription_id=1,
        worker_id=3,
        public_name="test-user",
        external_uuid="11111111-1111-1111-1111-111111111111",
    )

    command = build_vpn_client_revoke_command(worker, access_key)

    assert "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked" in command
    assert "client_traffics" in command
    assert "client_inbounds" in command
    assert "clients" in command
    assert "dropcatch-12-test-user" in command
    assert "11111111-1111-1111-1111-111111111111" in command


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


@pytest.mark.asyncio
async def test_revoke_vpn_access_key_marks_revoked_on_success(monkeypatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def get(self, *args, **kwargs):
            return None

        def add(self, item: object) -> None:
            self.added.append(item)

    async def fake_ssh(*args, **kwargs) -> str:
        return "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked\n"

    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", fake_ssh)
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
        status="active",
        public_name="phone",
        external_uuid="11111111-1111-1111-1111-111111111111",
    )

    session = FakeSession()
    result = await revoke_vpn_access_key(session, access_key, worker=worker)  # type: ignore[arg-type]

    assert result is access_key
    assert access_key.status == "revoked"
    assert access_key.revoked_at is not None
    assert access_key.last_error is None
    event = next(item for item in session.added if isinstance(item, VpnNodeEvent))
    assert event.event_type == "client_revoked"


@pytest.mark.asyncio
async def test_revoke_vpn_access_key_without_config_marks_pending_revoke() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def get(self, *args, **kwargs):
            return None

        def add(self, item: object) -> None:
            self.added.append(item)

    worker = WorkerNode(id=7, name="vpn-node", ip_address="31.77.157.65")
    access_key = VpnAccessKey(
        id=12,
        subscription_id=1,
        worker_id=7,
        status="active",
        public_name="phone",
        external_uuid="11111111-1111-1111-1111-111111111111",
    )

    session = FakeSession()
    result = await revoke_vpn_access_key(session, access_key, worker=worker)  # type: ignore[arg-type]

    assert result is access_key
    assert access_key.status == "pending_revoke"
    assert access_key.last_error
    event = next(item for item in session.added if isinstance(item, VpnNodeEvent))
    assert event.event_type == "client_revoke_pending"


class ProvisionSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def get(self, *args, **kwargs):
        return None

    async def flush(self) -> None:
        pass

    def add(self, item: object) -> None:
        self.added.append(item)


def existing_access_key():
    return VpnAccessKey(
        id=12, subscription_id=1, worker_id=7, status="active", public_name="phone",
        protocol="vless", external_uuid="11111111-1111-1111-1111-111111111111",
        config_uri="vless://existing-private-config", revoked_at=None,
    )


def configured_worker():
    return WorkerNode(
        id=7, name="vpn-node", ip_address="vpn.example.test", ssh_password="ssh-secret",
        vpn_panel_password="panel-secret", vpn_inbound_id=1,
    )


def suspend_function():
    suspend = getattr(vpn_provisioning, "suspend_vpn_access_key", None)
    assert callable(suspend), "reversible suspension service is missing"
    return suspend


@pytest.mark.asyncio
async def test_suspend_preserves_identity_and_revocation_metadata(monkeypatch):
    async def ssh(*args, **kwargs):
        return "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended\n"

    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", ssh)
    access_key = existing_access_key()
    identity = (access_key.external_uuid, access_key.config_uri, access_key.revoked_at)
    session = ProvisionSession()
    result = await suspend_function()(session, access_key, worker=configured_worker())
    assert result is access_key
    assert access_key.status == "suspended"
    assert (access_key.external_uuid, access_key.config_uri, access_key.revoked_at) == identity
    assert access_key.last_error is None
    assert access_key.last_synced_at is not None
    assert session.added[-1].event_type == "client_suspended"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ssh", "missing-marker", "misleading-marker"])
async def test_suspend_failure_is_pending_and_sanitizes_secrets(monkeypatch, failure):
    access_key = existing_access_key()
    worker = configured_worker()

    async def ssh(*args, **kwargs):
        log = f"{worker.ssh_password} {worker.vpn_panel_password} {access_key.external_uuid} {access_key.config_uri}"
        if failure == "ssh":
            raise RuntimeError(log)
        if failure == "misleading-marker":
            return "not-DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended\n" + log
        return log

    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", ssh)
    session = ProvisionSession()
    await suspend_function()(session, access_key, worker=worker)
    assert access_key.status == "pending_suspend"
    assert access_key.revoked_at is None
    assert access_key.config_uri == "vless://existing-private-config"
    assert access_key.last_error
    for secret in (worker.ssh_password, worker.vpn_panel_password, access_key.external_uuid, access_key.config_uri):
        assert secret not in access_key.last_error
        assert secret not in session.added[-1].message
    assert session.added[-1].event_type == "client_suspend_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["worker", "ssh", "inbound"])
async def test_suspend_unavailable_worker_remains_pending(missing):
    worker = configured_worker()
    if missing == "worker":
        worker = None
    elif missing == "ssh":
        worker.ssh_password = None
    else:
        worker.vpn_inbound_id = None
    access_key = existing_access_key()
    await suspend_function()(ProvisionSession(), access_key, worker=worker)
    assert access_key.status == "pending_suspend"
    assert access_key.last_error
    assert access_key.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_provision_requires_success_marker_and_preserves_existing_uri(monkeypatch, confirmed):
    async def ssh(*args, **kwargs):
        prefix = "DROPCATCH_VPN_CLIENT_STATUS=provisioned\n" if confirmed else ""
        return prefix + "DROPCATCH_VPN_CLIENT_URL=vless://newly-generated-config\n"

    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", ssh)
    access_key = existing_access_key()
    subscription = VpnSubscription(status="active", max_devices=3, traffic_limit_gb=25)
    await provision_vpn_access_key(ProvisionSession(), access_key, worker=configured_worker(), subscription=subscription)
    assert access_key.status == ("active" if confirmed else "pending_sync")
    assert access_key.config_uri == "vless://existing-private-config"
    assert access_key.revoked_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_first_attempt", [False, True])
async def test_legacy_uuid_migration_replaces_uri_after_success_even_on_retry(monkeypatch, fail_first_attempt):
    access_key = existing_access_key()
    access_key.external_uuid = "legacy-invalid-uuid"
    access_key.config_uri = "vless://legacy-invalid-uuid@vpn.example.test:443"
    calls = 0

    async def ssh(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and fail_first_attempt:
            raise RuntimeError("temporary SSH failure")
        return ("DROPCATCH_VPN_CLIENT_STATUS=provisioned\n"
                f"DROPCATCH_VPN_CLIENT_URL=vless://{access_key.external_uuid}@vpn.example.test:443\n")

    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", ssh)
    session = ProvisionSession()
    subscription = VpnSubscription(status="active", max_devices=3, traffic_limit_gb=25)
    worker = configured_worker()
    await provision_vpn_access_key(session, access_key, worker=worker, subscription=subscription)
    migrated_uuid = access_key.external_uuid
    assert str(UUID(migrated_uuid)) == migrated_uuid
    if fail_first_attempt:
        assert access_key.status == "pending_sync"
        assert access_key.config_uri is None, "failed migration must not retain a URI for the old credential"
        await provision_vpn_access_key(session, access_key, worker=worker, subscription=subscription)
    assert access_key.status == "active"
    assert access_key.external_uuid == migrated_uuid
    assert access_key.config_uri == f"vless://{migrated_uuid}@vpn.example.test:443"
