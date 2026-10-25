"""Preserve invitations and their credentials when removing the cohort limit."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_friend_invitations import NOW

from app.db.base import Base
from app.db.migrations import (
    MIGRATIONS,
    VPN_FRIEND_INVITATION_MIGRATIONS,
    VPN_UNLIMITED_INVITATION_MIGRATIONS,
)
from app.db.models import (
    User,
    VpnAccessKey,
    VpnCustomer,
    VpnFriendInvitation,
    VpnSubscription,
)


async def upgrade(engine, statements=VPN_UNLIMITED_INVITATION_MIGRATIONS):
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_before_upgrade", [False, True])
async def test_old_redeemed_invitation_survives_upgrade_and_sequence_does_not_rewind(
    postgres_schema, metadata_before_upgrade
):
    engine = postgres_schema.engine
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("DROP TABLE vpn_friend_invitations"))
    await upgrade(engine, VPN_FRIEND_INVITATION_MIGRATIONS)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                User(
                    id=1, username="synthetic", password_hash="synthetic", role="admin"
                ),
                VpnCustomer(id=1, telegram_user_id="42", status="active"),
            ]
        )
        await db.flush()
        db.add(
            VpnSubscription(
                id=1,
                customer_id=1,
                status="trial",
                starts_at=NOW,
                expires_at=NOW + timedelta(days=7),
            )
        )
        await db.flush()
        db.add(
            VpnAccessKey(
                id=1,
                subscription_id=1,
                status="active",
                external_uuid="synthetic-unchanged",
                config_uri="vless://synthetic-unchanged@vpn.example:443#unchanged",
                expires_at=NOW + timedelta(days=7),
            )
        )
        await db.flush()
        db.add(
            VpnFriendInvitation(
                slot=10,
                token_digest="a" * 64,
                created_by_user_id=1,
                created_at=NOW,
                redeem_expires_at=NOW + timedelta(days=7),
                redeemed_at=NOW,
                telegram_user_id="42",
                access_key_id=1,
            )
        )
        await db.commit()
    async with engine.begin() as connection:
        before = await connection.scalar(
            text(
                "SELECT row_to_json(i)::text FROM vpn_friend_invitations i WHERE slot=10"
            )
        )
        credential = await connection.scalar(
            text("SELECT row_to_json(k)::text FROM vpn_access_keys k WHERE id=1")
        )
        if metadata_before_upgrade:
            await connection.run_sync(Base.metadata.create_all)
    await upgrade(engine)
    await upgrade(engine)
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT row_to_json(i)::text FROM vpn_friend_invitations i WHERE slot=10"
                )
            )
            == before
        )
        assert (
            await connection.scalar(
                text("SELECT row_to_json(k)::text FROM vpn_access_keys k WHERE id=1")
            )
            == credential
        )
        assert (
            await connection.scalar(
                text("""
            INSERT INTO vpn_friend_invitations (token_digest, redeem_expires_at)
            VALUES (:digest, :expires) RETURNING slot
        """),
                {"digest": "b" * 64, "expires": NOW + timedelta(days=7)},
            )
            == 11
        )
        await connection.execute(
            text(
                "SELECT setval('vpn_friend_invitations_slot_seq'::regclass, 60000, true)"
            )
        )
    await upgrade(engine)
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text("""
            INSERT INTO vpn_friend_invitations (token_digest, redeem_expires_at)
            VALUES (:digest, :expires) RETURNING slot
        """),
                {"digest": "c" * 64, "expires": NOW + timedelta(days=7)},
            )
            == 60001
        )
        for slot, digest in ((0, "d" * 64), (-1, "e" * 64), (60002, "a" * 64)):
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                        INSERT INTO vpn_friend_invitations (slot, token_digest, redeem_expires_at)
                        VALUES (:slot, :digest, :expires)
                    """),
                        {
                            "slot": slot,
                            "digest": digest,
                            "expires": NOW + timedelta(days=7),
                        },
                    )


@pytest.mark.asyncio
async def test_fresh_metadata_and_full_migrations_keep_generated_positive_ids(
    postgres_schema,
):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await upgrade(postgres_schema.engine, MIGRATIONS)
    await upgrade(postgres_schema.engine, MIGRATIONS)
    async with postgres_schema.engine.begin() as connection:
        assert (
            await connection.scalar(
                text("""
            INSERT INTO vpn_friend_invitations (token_digest, redeem_expires_at)
            VALUES (:digest, :expires) RETURNING slot
        """),
                {"digest": "f" * 64, "expires": NOW + timedelta(days=7)},
            )
            == 1
        )
