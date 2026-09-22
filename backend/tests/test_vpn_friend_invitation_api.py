from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes import control as control_routes
from app.db.base import Base
from app.db.models import AdminAuditLog
from app.db.session import get_db
from app.services.vpn_friend_invitations import (
    FriendInvitationConflict,
    FriendInvitationUnavailable,
    FriendInvitationView,
    IssuedFriendInvitation,
)
from app.services.vpn_mutations import serialize_vpn_mutation


LIST_URL = "/api/control/vpn/friend-invitations"


def _view(slot: int, *, state: str = "unused") -> FriendInvitationView:
    return FriendInvitationView(
        slot=slot,
        invite_state=state,
        telegram_user_id=None,
        telegram_username=None,
        display_name=None,
        subscription_expires_at=None,
        provisioning_error_code=None,
        can_rotate=state == "unused",
        can_retry=False,
        can_disable=False,
    )


async def _database():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _app(session_factory, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(control_routes.router, prefix="/api")

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    if authenticated:
        async def fake_admin():
            return SimpleNamespace(id=7, role="owner")

        app.dependency_overrides[require_admin] = fake_admin
    return app


@pytest.mark.asyncio
async def test_friend_invitation_routes_require_admin_authentication():
    engine, session_factory = await _database()
    app = _app(session_factory, authenticated=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        responses = (
            await client.get(LIST_URL),
            await client.post(LIST_URL),
            await client.post(f"{LIST_URL}/1/rotate"),
        )

    assert [response.status_code for response in responses] == [401, 401, 401]
    await engine.dispose()


@pytest.mark.asyncio
async def test_friend_invitation_admin_contract_serializes_mutations_and_redacts_audit(
    monkeypatch,
):
    engine, session_factory = await _database()
    app = _app(session_factory)
    serialized: list[str] = []
    raw_token = "raw-token-that-must-not-be-persisted"
    invite_link = f"https://t.me/veltrix_vpn_official_bot?start=i_{raw_token}"

    async def serialized_mutation():
        serialized.append("enter")
        yield
        serialized.append("exit")

    async def fake_list(db, now):
        del db
        assert now.tzinfo is not None
        return [_view(slot) for slot in range(1, 11)]

    async def fake_issue(db, settings, actor_user_id, now):
        del db, settings
        assert actor_user_id == 7
        assert now.tzinfo is not None
        return IssuedFriendInvitation(view=_view(1), link=invite_link)

    async def fake_rotate(db, settings, slot, actor_user_id, now):
        del db, settings
        assert slot == 1
        assert actor_user_id == 7
        assert now.tzinfo is not None
        return IssuedFriendInvitation(view=_view(1), link=invite_link)

    app.dependency_overrides[serialize_vpn_mutation] = serialized_mutation
    monkeypatch.setattr(control_routes, "list_friend_invitations", fake_list)
    monkeypatch.setattr(control_routes, "issue_friend_invitation", fake_issue)
    monkeypatch.setattr(control_routes, "rotate_friend_invitation", fake_rotate)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        listed = await client.get(LIST_URL)
        created = await client.post(LIST_URL)
        rotated = await client.post(f"{LIST_URL}/1/rotate")
        listed_again = await client.get(LIST_URL)

    assert listed.status_code == 200
    assert len(listed.json()) == 10
    assert "invite_link" not in listed.text
    assert "invite_link" not in listed_again.text
    assert serialized == ["enter", "exit", "enter", "exit"]

    expected_fields = {
        "slot",
        "invite_state",
        "telegram_user_id",
        "telegram_username",
        "display_name",
        "subscription_expires_at",
        "provisioning_error_code",
        "can_rotate",
        "can_retry",
        "can_disable",
    }
    for response, expected_status in ((created, 201), (rotated, 200)):
        assert response.status_code == expected_status
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert set(payload) == {"invitation", "invite_link"}
        assert set(payload["invitation"]) == expected_fields
        assert payload["invite_link"] == invite_link

    async with session_factory() as session:
        audit_rows = (
            await session.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id))
        ).all()
    assert [(row.action, row.details) for row in audit_rows] == [
        ("vpn_friend_invitation_issue", "slot=1"),
        ("vpn_friend_invitation_rotate", "slot=1"),
    ]
    assert all(raw_token not in (row.details or "") for row in audit_rows)
    assert all(invite_link not in (row.details or "") for row in audit_rows)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "service_error", "expected_status", "expected_detail"),
    [
        (
            LIST_URL,
            FriendInvitationConflict("secret-token-conflict"),
            409,
            "Friend invitation cannot be changed",
        ),
        (
            LIST_URL,
            FriendInvitationUnavailable("secret-token-unavailable"),
            503,
            "Friend invitations are temporarily unavailable",
        ),
        (
            f"{LIST_URL}/1/rotate",
            FriendInvitationConflict("secret-token-conflict"),
            409,
            "Friend invitation cannot be changed",
        ),
        (
            f"{LIST_URL}/1/rotate",
            FriendInvitationUnavailable("secret-token-unavailable"),
            503,
            "Friend invitations are temporarily unavailable",
        ),
    ],
)
async def test_friend_invitation_errors_are_bounded_and_not_cached(
    monkeypatch,
    path,
    service_error,
    expected_status,
    expected_detail,
):
    engine, session_factory = await _database()
    app = _app(session_factory)

    async def fail_invitation(*args, **kwargs):
        del args, kwargs
        raise service_error

    service_name = (
        "rotate_friend_invitation" if path.endswith("/rotate")
        else "issue_friend_invitation"
    )
    monkeypatch.setattr(control_routes, service_name, fail_invitation)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(path)

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    assert "secret-token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "handler_arguments", "service_name"),
    [
        (
            "create_vpn_friend_invitation",
            {},
            "issue_friend_invitation",
        ),
        (
            "rotate_vpn_friend_invitation",
            {"slot": 1},
            "rotate_friend_invitation",
        ),
    ],
)
async def test_friend_invitation_handlers_suppress_service_exception_cause(
    monkeypatch,
    handler_name,
    handler_arguments,
    service_name,
):
    secret = "raw-invitation-secret-must-not-reach-error-tracking"

    async def fail_invitation(*args, **kwargs):
        del args, kwargs
        raise FriendInvitationConflict(secret)

    monkeypatch.setattr(control_routes, service_name, fail_invitation)

    with pytest.raises(HTTPException) as caught:
        await getattr(control_routes, handler_name)(
            response=Response(),
            db=object(),
            admin=SimpleNamespace(id=7, role="owner"),
            **handler_arguments,
        )

    assert caught.value.__cause__ is None
    assert secret not in str(caught.value)
