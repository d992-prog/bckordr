from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    AppSetting,
    User,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnFriendInvitation,
    VpnSubscription,
    WorkerNode,
)
from app.services import vpn_friend_invitations as invitations
from app.services.vpn_friend_invitations import (
    FriendInvitationConflict,
    FriendInvitationUnavailable,
    digest_invite_token,
    issue_friend_invitation,
    list_friend_invitations,
    rotate_friend_invitation,
)
from app.services.vpn_node_transport import VpnNodeTransportError


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
RELEASE_ID = "a" * 64
READINESS_KEY = "vpn_friend_beta_release_ready_v1"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "VPN_FRIEND_BETA_ENABLED": True,
        "VPN_FRIEND_BETA_RELEASE_ID": RELEASE_ID,
        "VPN_TELEGRAM_BOT_USERNAME": "veltrix_vpn_official_bot",
        "VPN_CONTROL_DISPATCH_ENABLED": True,
        "VPN_CONTROL_KNOWN_HOSTS_PATH": "C:/veltrix/known_hosts",
        "VPN_PORTAL_PUBLIC_ACCESS": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'friend-invitations.sqlite3'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_ready_environment(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="owner",
                    password_hash="hash",
                    role="admin",
                    status="active",
                ),
                User(
                    id=2,
                    username="other-admin",
                    password_hash="hash",
                    role="admin",
                    status="active",
                ),
                AppSetting(key=READINESS_KEY, value=RELEASE_ID),
                WorkerNode(
                    id=1,
                    name="friend-beta-worker",
                    status="ready",
                    is_enabled=True,
                    vpn_enabled=True,
                    vpn_role="vpn_node",
                    vpn_runtime_status="ready",
                    ssh_host="192.0.2.10",
                    ssh_port=22,
                    ssh_username="root",
                    ssh_password="test-only-password",
                ),
            ]
        )
        await db.flush()
        db.add(
            VpnEndpoint(
                id=1,
                worker_id=1,
                inbound_id=11,
                public_host="vpn.example.test",
                port=443,
                protocol="vless",
                transport="raw",
                security="reality",
                server_name="cdn.example.test",
                public_key="A" * 43,
                short_id="0123456789abcdef",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                status="ready",
                verified_at=NOW - timedelta(hours=1),
            )
        )
        await db.commit()


@pytest_asyncio.fixture
async def ready_session(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncSession, Settings, list[tuple[int, Path]]]]:
    await _seed_ready_environment(session_factory)
    transport_calls: list[tuple[int, Path]] = []

    def _valid_transport(worker: WorkerNode, path: Path) -> object:
        transport_calls.append((worker.id, path))
        return object()

    monkeypatch.setattr(invitations, "load_transport_snapshot", _valid_transport)
    async with session_factory() as db:
        yield db, _settings(), transport_calls
        await db.rollback()


def _token_from_link(link: str) -> str:
    query = parse_qs(urlsplit(link).query)
    return query["start"][0].removeprefix("i_")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@pytest.mark.asyncio
async def test_issue_returns_raw_link_once_and_list_returns_ten_safe_slots(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, transport_calls = ready_session

    issued = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    raw_token = _token_from_link(issued.link)
    invitation = await db.scalar(
        select(VpnFriendInvitation).where(VpnFriendInvitation.slot == 1)
    )
    views = await list_friend_invitations(db, NOW)

    assert issued.link.startswith(
        "https://t.me/veltrix_vpn_official_bot?start=i_"
    )
    assert invitation is not None
    assert raw_token not in invitation.token_digest
    assert len(invitation.token_digest) == 64
    assert invitation.token_digest == digest_invite_token(raw_token)
    assert invitation.token_digest != hashlib.sha256(raw_token.encode("ascii")).hexdigest()
    assert invitation.created_by_user_id == 1
    assert _utc(invitation.created_at) == NOW
    assert _utc(invitation.redeem_expires_at) == NOW + timedelta(days=7)
    assert len(views) == 10
    assert [view.slot for view in views] == list(range(1, 11))
    assert views[0] == issued.view
    assert views[0].invite_state == "unused"
    assert views[0].can_rotate is True
    assert views[1].invite_state == "unused"
    assert views[1].can_rotate is False
    assert not hasattr(views[0], "token")
    assert not hasattr(views[0], "link")
    assert raw_token not in repr(views)
    assert issued.link not in repr(views)
    assert transport_calls == [(1, Path(settings.vpn_control_known_hosts_path))]


@pytest.mark.asyncio
async def test_issued_invitation_repr_excludes_the_one_time_secret(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session

    issued = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    raw_token = _token_from_link(issued.link)

    assert issued.link.startswith(
        "https://t.me/veltrix_vpn_official_bot?start=i_"
    )
    assert issued.link not in repr(issued)
    assert raw_token not in repr(issued)


@pytest.mark.parametrize(
    "username",
    [
        "",
        "bot",
        "@veltrix_bot",
        "veltrix-bot",
        "veltrix_vpn_official",
        "a" * 33,
        "бот_bot",
    ],
)
@pytest.mark.asyncio
async def test_issue_rejects_invalid_bot_usernames_with_one_bounded_error(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    username: str,
) -> None:
    db, settings, _ = ready_session
    settings.vpn_telegram_bot_username = username

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert await db.scalar(select(VpnFriendInvitation)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vpn_friend_beta_enabled", False),
        ("vpn_control_dispatch_enabled", False),
        ("vpn_portal_public_access", True),
        ("vpn_friend_beta_release_id", ""),
        ("vpn_friend_beta_release_id", "A" * 64),
        ("vpn_friend_beta_release_id", "a" * 63),
    ],
)
@pytest.mark.asyncio
async def test_issue_fails_closed_for_every_configuration_gate(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    field: str,
    value: object,
) -> None:
    db, settings, transport_calls = ready_session
    setattr(settings, field, value)

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert transport_calls == []


@pytest.mark.parametrize("marker", [None, "b" * 64])
@pytest.mark.asyncio
async def test_issue_fails_closed_for_absent_or_mismatched_private_marker(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    marker: str | None,
) -> None:
    db, settings, transport_calls = ready_session
    setting = await db.scalar(
        select(AppSetting).where(AppSetting.key == READINESS_KEY)
    )
    assert setting is not None
    if marker is None:
        await db.delete(setting)
    else:
        setting.value = marker
    await db.flush()

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert transport_calls == []


@pytest.mark.asyncio
async def test_readiness_reads_marker_endpoint_and_worker_in_one_atomic_query(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session
    statements: list[str] = []

    def _capture_select(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _capture_select)
    try:
        endpoint = await invitations.require_friend_invitation_readiness(db, settings)
    finally:
        event.remove(engine, "before_cursor_execute", _capture_select)

    assert endpoint.id == 1
    assert len(statements) == 1
    statement = statements[0]
    assert "app_settings" in statement
    assert "vpn_endpoints" in statement
    assert "worker_nodes" in statement


@pytest.mark.parametrize(
    ("status", "security", "verified_at"),
    [
        ("staged", "reality", NOW),
        ("ready", "tls", NOW),
        ("ready", "reality", None),
    ],
)
@pytest.mark.asyncio
async def test_issue_requires_a_ready_verified_reality_endpoint(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    status: str,
    security: str,
    verified_at: datetime | None,
) -> None:
    db, settings, transport_calls = ready_session
    endpoint = await db.get(VpnEndpoint, 1)
    assert endpoint is not None
    ignore_check = status == "ready" and security == "reality" and verified_at is None
    if ignore_check:
        await db.execute(text("PRAGMA ignore_check_constraints = ON"))
    try:
        endpoint.status = status
        endpoint.security = security
        endpoint.verified_at = verified_at
        await db.flush()
    finally:
        if ignore_check:
            await db.execute(text("PRAGMA ignore_check_constraints = OFF"))

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert transport_calls == []


@pytest.mark.asyncio
async def test_issue_maps_invalid_strict_transport_to_the_same_bounded_error(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, settings, _ = ready_session

    def _invalid_transport(_worker: WorkerNode, _path: Path) -> object:
        raise VpnNodeTransportError("preflight")

    monkeypatch.setattr(invitations, "load_transport_snapshot", _invalid_transport)

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_eleventh_sequential_issue_is_rejected_without_mutating_ten_slots(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session

    issued = [
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
        for _ in range(10)
    ]
    with pytest.raises(FriendInvitationConflict) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    rows = (await db.scalars(select(VpnFriendInvitation))).all()
    assert [item.view.slot for item in issued] == list(range(1, 11))
    assert str(error.value) == "friend_invitation_cohort_full"
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_issue_regenerates_a_token_digest_collision_in_the_same_free_slot(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, settings, _ = ready_session
    tokens = iter(["A" * 43, "A" * 43, "B" * 43])
    monkeypatch.setattr(invitations.secrets, "token_urlsafe", lambda _size: next(tokens))

    first = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    second = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert first.view.slot == 1
    assert second.view.slot == 2
    assert _token_from_link(first.link) == "A" * 43
    assert _token_from_link(second.link) == "B" * 43


@pytest.mark.asyncio
async def test_rotation_keeps_slot_and_audit_owner_but_replaces_secret_window(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session
    original = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    original_digest = digest_invite_token(_token_from_link(original.link))
    rotated_at = NOW + timedelta(days=8)

    rotated = await rotate_friend_invitation(
        db,
        settings,
        slot=1,
        actor_user_id=2,
        now=rotated_at,
    )
    invitation = await db.get(VpnFriendInvitation, 1)
    views = await list_friend_invitations(db, rotated_at)

    assert invitation is not None
    assert rotated.view.slot == 1
    assert invitation.slot == 1
    assert invitation.token_digest != original_digest
    assert invitation.token_digest == digest_invite_token(_token_from_link(rotated.link))
    assert invitation.created_by_user_id == 1
    assert _utc(invitation.created_at) == rotated_at
    assert _utc(invitation.redeem_expires_at) == rotated_at + timedelta(days=7)
    assert original.link not in repr(views)
    assert rotated.link not in repr(views)


async def _bind_invitation(
    db: AsyncSession,
    invitation: VpnFriendInvitation,
    *,
    slot: int,
    telegram_user_id: str | None = None,
    customer_telegram_user_id: str | None = None,
    operation_state: str = "queued",
    operation_error: str | None = None,
    subscription_expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    latest_generation: int = 1,
) -> None:
    invited_id = telegram_user_id or str(100_000 + slot)
    customer_id = customer_telegram_user_id or invited_id
    expires_at = subscription_expires_at or NOW + timedelta(days=7)
    customer = VpnCustomer(
        id=slot,
        telegram_user_id=customer_id,
        telegram_username=f"friend{slot}",
        first_name=f"Friend {slot}",
        last_name="Veltrix",
        status="active",
    )
    db.add(customer)
    await db.flush()
    subscription = VpnSubscription(
        id=slot,
        customer_id=customer.id,
        status="trial",
        starts_at=NOW - timedelta(hours=1),
        expires_at=expires_at,
        max_devices=1,
    )
    db.add(subscription)
    await db.flush()
    access_key = VpnAccessKey(
        id=slot,
        subscription_id=subscription.id,
        worker_id=1,
        endpoint_id=1,
        operation_generation=latest_generation,
        protocol="vless",
        external_uuid=str(UUID(int=slot)),
        config_uri=(
            f"vless://{UUID(int=slot)}@vpn.example.test:443"
            if operation_state == "succeeded"
            else None
        ),
        status="active" if operation_state == "succeeded" else "pending_sync",
        issued_at=NOW,
        expires_at=expires_at,
    )
    db.add(access_key)
    await db.flush()
    invitation.redeemed_at = NOW
    invitation.telegram_user_id = invited_id
    invitation.access_key_id = access_key.id
    invitation.revoked_at = revoked_at
    await db.flush()
    if latest_generation > 1:
        db.add(
            VpnControlOperation(
                id=str(UUID(int=slot * 100)),
                access_key_id=access_key.id,
                worker_id=1,
                endpoint_id=1,
                generation=latest_generation - 1,
                action="provision",
                request_snapshot={"generation": latest_generation - 1},
                request_digest=f"{slot * 100:064x}",
                state="failed",
                error_code="vpn_node_preflight_failed",
            )
        )
    db.add(
        VpnControlOperation(
            id=str(UUID(int=slot * 100 + latest_generation)),
            access_key_id=access_key.id,
            worker_id=1,
            endpoint_id=1,
            generation=latest_generation,
            action="provision",
            request_snapshot={"generation": latest_generation},
            request_digest=f"{slot * 100 + latest_generation:064x}",
            state=operation_state,
            claim_token=(str(UUID(int=slot * 1000)) if operation_state == "uncertain" else None),
            claimed_at=(NOW if operation_state == "uncertain" else None),
            error_code=operation_error,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_rotation_rejects_redeemed_revoked_and_never_issued_slots(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session
    redeemed = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    redeemed_row = await db.get(VpnFriendInvitation, redeemed.view.slot)
    assert redeemed_row is not None
    await _bind_invitation(db, redeemed_row, slot=redeemed.view.slot)
    revoked = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    revoked_row = await db.get(VpnFriendInvitation, revoked.view.slot)
    assert revoked_row is not None
    revoked_row.revoked_at = NOW
    await db.flush()

    for slot in (redeemed.view.slot, revoked.view.slot, 10):
        with pytest.raises(FriendInvitationConflict) as error:
            await rotate_friend_invitation(
                db,
                settings,
                slot=slot,
                actor_user_id=2,
                now=NOW + timedelta(hours=1),
            )
        assert str(error.value) == "friend_invitation_not_rotatable"


@pytest.mark.asyncio
async def test_rotation_regenerates_a_colliding_digest(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, settings, _ = ready_session
    tokens = iter(["A" * 43, "B" * 43, "B" * 43, "C" * 43])
    monkeypatch.setattr(invitations.secrets, "token_urlsafe", lambda _size: next(tokens))
    await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    rotated = await rotate_friend_invitation(
        db,
        settings,
        slot=1,
        actor_user_id=2,
        now=NOW + timedelta(hours=1),
    )

    assert _token_from_link(rotated.link) == "C" * 43


@pytest.mark.asyncio
async def test_list_derives_all_states_from_exact_chain_and_latest_operation(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, _ = ready_session
    issued = [
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
        for _ in range(10)
    ]
    rows = {
        row.slot: row
        for row in (
            await db.scalars(
                select(VpnFriendInvitation).order_by(VpnFriendInvitation.slot)
            )
        ).all()
    }
    rows[3].created_at = NOW - timedelta(days=8)
    rows[3].redeem_expires_at = NOW - timedelta(days=1)
    await _bind_invitation(db, rows[4], slot=4, operation_state="queued")
    await _bind_invitation(
        db,
        rows[5],
        slot=5,
        operation_state="succeeded",
        latest_generation=2,
    )
    await _bind_invitation(
        db,
        rows[6],
        slot=6,
        operation_state="failed",
        operation_error="vpn_node_preflight_failed",
    )
    await _bind_invitation(
        db,
        rows[7],
        slot=7,
        operation_state="uncertain",
        operation_error="vpn_node_mutation_uncertain",
    )
    await _bind_invitation(
        db,
        rows[8],
        slot=8,
        operation_state="succeeded",
        revoked_at=NOW,
    )
    await _bind_invitation(
        db,
        rows[9],
        slot=9,
        telegram_user_id="900009",
        customer_telegram_user_id="900099",
        operation_state="succeeded",
        revoked_at=NOW,
    )
    await _bind_invitation(
        db,
        rows[10],
        slot=10,
        operation_state="succeeded",
        subscription_expires_at=NOW,
    )

    views = await list_friend_invitations(db, NOW)

    assert [item.invite_state for item in views] == [
        "unused",
        "unused",
        "expired",
        "preparing",
        "active",
        "failed",
        "needs_verification",
        "disabled",
        "failed",
        "expired",
    ]
    assert views[0].can_rotate is True
    assert views[1].can_rotate is True
    assert views[2].can_rotate is True
    assert views[4].telegram_user_id == "100005"
    assert views[4].telegram_username == "friend5"
    assert views[4].display_name == "Friend 5 Veltrix"
    assert views[4].subscription_expires_at == NOW + timedelta(days=7)
    assert views[5].provisioning_error_code == "vpn_node_preflight_failed"
    assert views[5].can_retry is True
    assert views[6].can_retry is False
    assert views[6].can_disable is True
    assert views[7].can_disable is False
    assert views[8].telegram_user_id == "900009"
    assert views[8].telegram_username is None
    assert views[8].subscription_expires_at is None
    assert views[8].can_disable is False
    assert len(issued) == 10


@pytest.mark.parametrize(
    ("disabled_by", "expected_state"),
    [
        ("invitation", "disabled"),
        ("customer", "disabled"),
        ("subscription", "disabled"),
        ("key", "disabled"),
        ("expiry", "expired"),
    ],
)
@pytest.mark.asyncio
async def test_retryable_failure_cannot_retry_when_invitation_is_not_failed(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
    disabled_by: str,
    expected_state: str,
) -> None:
    db, settings, _ = ready_session
    issued = await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    invitation = await db.get(VpnFriendInvitation, issued.view.slot)
    assert invitation is not None
    await _bind_invitation(
        db,
        invitation,
        slot=issued.view.slot,
        operation_state="failed",
        operation_error="vpn_node_preflight_failed",
    )
    access_key = await db.get(VpnAccessKey, issued.view.slot)
    subscription = await db.get(VpnSubscription, issued.view.slot)
    customer = await db.get(VpnCustomer, issued.view.slot)
    assert access_key is not None
    assert subscription is not None
    assert customer is not None

    if disabled_by == "invitation":
        invitation.revoked_at = NOW
    elif disabled_by == "customer":
        customer.status = "archived"
    elif disabled_by == "subscription":
        subscription.status = "disabled"
    elif disabled_by == "key":
        access_key.status = "revoked"
        access_key.revoked_at = NOW
    else:
        subscription.expires_at = NOW
    await db.flush()

    view = (await list_friend_invitations(db, NOW))[0]

    assert view.invite_state == expected_state
    assert view.can_retry is False


@pytest.mark.asyncio
async def test_deleting_the_only_ready_endpoint_fails_closed(
    ready_session: tuple[AsyncSession, Settings, list[tuple[int, Path]]],
) -> None:
    db, settings, transport_calls = ready_session
    await db.execute(delete(VpnEndpoint))

    with pytest.raises(FriendInvitationUnavailable) as error:
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)

    assert str(error.value) == "friend_beta_unavailable"
    assert transport_calls == []
