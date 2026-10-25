"""Invitations have individual one-use identities, not a ten-person cohort."""

import pytest
from sqlalchemy import CheckConstraint, select
from test_vpn_friend_invitations import NOW
from test_vpn_friend_invitations import ready_session as ready_session  # noqa: PLC0414
from test_vpn_friend_invitations import (
    session_factory as session_factory,  # noqa: PLC0414
)

from app.db.models import VpnFriendInvitation
from app.services.vpn_friend_invitations import (
    issue_friend_invitation,
    list_friend_invitations,
)


def test_invitation_identity_is_a_positive_generated_integer():
    table = VpnFriendInvitation.__table__
    assert str(table.c.slot.type) == "INTEGER"
    assert table.c.slot.autoincrement is True
    assert any(
        constraint.name == "ck_vpn_friend_invitation_slot"
        and str(constraint.sqltext) == "slot > 0"
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )


@pytest.mark.asyncio
async def test_invitation_count_is_not_limited_to_ten(ready_session):
    db, settings, _ = ready_session
    issued = [
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
        for _ in range(11)
    ]
    assert len({item.view.slot for item in issued}) == 11
    assert max(item.view.slot for item in issued) > 10
    assert len({item.link for item in issued}) == 11
    assert len((await db.scalars(select(VpnFriendInvitation))).all()) == 11


@pytest.mark.asyncio
async def test_list_uses_descending_real_rows_and_exclusive_cursor(ready_session):
    db, settings, _ = ready_session
    assert await list_friend_invitations(db, NOW) == []
    for _ in range(5):
        await issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
    first = await list_friend_invitations(db, NOW, limit=2)
    second = await list_friend_invitations(db, NOW, before_slot=first[-1].slot, limit=2)
    last = await list_friend_invitations(db, NOW, before_slot=second[-1].slot, limit=2)
    assert [[row.slot for row in page] for page in (first, second, last)] == [
        [5, 4],
        [3, 2],
        [1],
    ]
    assert all(
        row.can_rotate and row.invite_state == "unused" for row in first + second + last
    )
    assert not any(hasattr(row, "link") for row in first + second + last)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"before_slot": 0},
        {"before_slot": True},
    ],
)
async def test_list_rejects_invalid_page_bounds(ready_session, options):
    db, _, _ = ready_session
    with pytest.raises(ValueError, match="invalid_invitation_page"):
        await list_friend_invitations(db, NOW, **options)
