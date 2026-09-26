from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from test_vpn_endpoint_migrations import PostgresSchema
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
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
    issue_friend_invitation,
    redeem_friend_invitation,
)
from app.services.vpn_telegram_identity import TelegramIdentity

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
RELEASE_ID = "a" * 64
READINESS_KEY = "vpn_friend_beta_release_ready_v1"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        VPN_FRIEND_BETA_ENABLED=True,
        VPN_FRIEND_BETA_RELEASE_ID=RELEASE_ID,
        VPN_TELEGRAM_BOT_USERNAME="veltrix_vpn_official_bot",
        VPN_CONTROL_DISPATCH_ENABLED=True,
        VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
        VPN_PORTAL_PUBLIC_ACCESS=False,
    )


@dataclass(frozen=True)
class PostgresFriendControl:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def postgres_friend_control(
    postgres_schema: PostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> PostgresFriendControl:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    control = PostgresFriendControl(
        engine=postgres_schema.engine,
        sessions=async_sessionmaker(
            postgres_schema.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        ),
    )
    monkeypatch.setattr(
        invitations,
        "load_transport_snapshot",
        lambda _worker, _path: object(),
    )
    async with control.sessions() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="owner",
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
                    vpn_public_host="vpn.example.test",
                    vpn_inbound_id=11,
                    ssh_host="192.0.2.10",
                    ssh_port=22,
                    ssh_username="root",
                    ssh_password="synthetic-test-password",
                    vpn_last_checked_at=NOW,
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
                verified_at=NOW - timedelta(minutes=1),
                max_active_profiles=4,
            )
        )
        await db.commit()
    return control


def _token(link: str) -> str:
    payload = parse_qs(urlsplit(link).query)["start"][0]
    return payload.removeprefix("i_")


async def _count(db: AsyncSession, model: type[object]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.asyncio
async def test_eleven_concurrent_issues_persist_exactly_ten_slots(
    postgres_friend_control: PostgresFriendControl,
) -> None:
    control = postgres_friend_control
    all_read_empty = asyncio.Event()
    observations = 0

    class RacingIssueSession(AsyncSession):
        async def scalars(self, statement, *args, **kwargs):
            nonlocal observations
            result = await super().scalars(statement, *args, **kwargs)
            if "vpn_friend_invitations.slot" in str(statement):
                observations += 1
                if observations == 11:
                    all_read_empty.set()
                await asyncio.wait_for(all_read_empty.wait(), timeout=30)
            return result

    racing_sessions = async_sessionmaker(
        control.engine,
        expire_on_commit=False,
        class_=RacingIssueSession,
    )

    async def contender() -> tuple[str, int | str]:
        async with racing_sessions() as db:
            try:
                issued = await issue_friend_invitation(
                    db,
                    _settings(),
                    actor_user_id=1,
                    now=NOW,
                )
            except FriendInvitationConflict as exc:
                await db.rollback()
                return "rejected", str(exc)
            await db.commit()
            return "issued", issued.view.slot

    results = await asyncio.wait_for(
        asyncio.gather(*(contender() for _ in range(11))),
        timeout=90,
    )

    async with control.sessions() as db:
        slots = (
            await db.scalars(
                select(VpnFriendInvitation.slot).order_by(VpnFriendInvitation.slot)
            )
        ).all()
    assert observations == 11
    assert sorted(value for state, value in results if state == "issued") == list(
        range(1, 11)
    )
    assert [value for state, value in results if state == "rejected"] == [
        "friend_invitation_cohort_full"
    ]
    assert slots == list(range(1, 11))


@pytest.mark.asyncio
async def test_two_identities_racing_one_token_have_one_winner_and_generic_rejection(
    postgres_friend_control: PostgresFriendControl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = postgres_friend_control
    token_with_internal_marker = "A" * 15 + "i_" + "A" * 26
    monkeypatch.setattr(
        invitations,
        "_new_token",
        lambda: (
            token_with_internal_marker,
            invitations.digest_invite_token(token_with_internal_marker),
        ),
    )
    async with control.sessions() as db:
        issued = await issue_friend_invitation(
            db,
            _settings(),
            actor_user_id=1,
            now=NOW,
        )
        await db.commit()
    token = _token(issued.link)
    start = asyncio.Event()

    async def contender(user_id: str) -> tuple[str, object]:
        async with control.sessions() as db:
            await start.wait()
            try:
                result = await redeem_friend_invitation(
                    db,
                    _settings(),
                    token,
                    TelegramIdentity(user_id, f"friend_{user_id}"),
                    NOW,
                )
            except FriendInvitationConflict as exc:
                await db.rollback()
                return "rejected", str(exc)
            await db.commit()
            return "redeemed", (user_id, result)

    contenders = [
        asyncio.create_task(contender("800001")),
        asyncio.create_task(contender("800002")),
    ]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*contenders), timeout=30)

    winners = [result for state, result in results if state == "redeemed"]
    rejections = [result for state, result in results if state == "rejected"]
    assert len(winners) == 1
    assert rejections == ["friend_invitation_rejected"]
    async with control.sessions() as db:
        invitation = await db.get(VpnFriendInvitation, 1)
        assert invitation is not None
        assert invitation.telegram_user_id in {"800001", "800002"}
        assert invitation.telegram_user_id == winners[0][0]
        assert await _count(db, VpnCustomer) == 1
        assert await _count(db, VpnSubscription) == 1
        assert await _count(db, VpnAccessKey) == 1
        assert await _count(db, VpnControlOperation) == 1


@pytest.mark.asyncio
async def test_endpoint_change_during_staging_rolls_back_entire_redemption(
    postgres_friend_control: PostgresFriendControl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = postgres_friend_control
    async with control.sessions() as db:
        issued = await issue_friend_invitation(
            db,
            _settings(),
            actor_user_id=1,
            now=NOW,
        )
        await db.commit()
    token = _token(issued.link)
    original_stage = invitations.stage_vpn_control_operation

    async def change_endpoint_then_stage(
        db: AsyncSession,
        access_key_id: int,
        action: str,
        *,
        now: datetime,
    ) -> VpnControlOperation:
        endpoint = await db.get(VpnEndpoint, 1)
        assert endpoint is not None
        endpoint.status = "disabled"
        await db.flush()
        return await original_stage(db, access_key_id, action, now=now)

    monkeypatch.setattr(
        invitations,
        "stage_vpn_control_operation",
        change_endpoint_then_stage,
    )

    async with control.sessions() as db:
        with pytest.raises(FriendInvitationUnavailable) as error:
            await redeem_friend_invitation(
                db,
                _settings(),
                token,
                TelegramIdentity("800003", "friend_800003"),
                NOW,
            )
        assert str(error.value) == "friend_beta_unavailable"
        await db.commit()

    async with control.sessions() as db:
        invitation = await db.get(VpnFriendInvitation, 1)
        endpoint = await db.get(VpnEndpoint, 1)
        assert invitation is not None
        assert invitation.redeemed_at is None
        assert invitation.telegram_user_id is None
        assert invitation.access_key_id is None
        assert endpoint is not None and endpoint.status == "ready"
        assert await _count(db, VpnCustomer) == 0
        assert await _count(db, VpnSubscription) == 0
        assert await _count(db, VpnAccessKey) == 0
        assert await _count(db, VpnControlOperation) == 0
