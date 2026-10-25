import importlib.util

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)

from app.db import models
from app.db.base import Base


def test_device_migration_is_registered_after_existing_migrations():
    assert importlib.util.find_spec("app.db.vpn_device_migrations") is not None
    from app.db.migrations import MIGRATIONS
    from app.db.vpn_device_migrations import VPN_DEVICE_MIGRATIONS
    assert MIGRATIONS[-len(VPN_DEVICE_MIGRATIONS):] == VPN_DEVICE_MIGRATIONS


async def migrate(engine):
    from app.db.vpn_device_migrations import VPN_DEVICE_MIGRATIONS
    async with engine.begin() as connection:
        for statement in VPN_DEVICE_MIGRATIONS:
            await connection.execute(text(statement))


async def legacy_schema(engine, *, metadata_first=False):
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE vpn_subscriptions (id SERIAL PRIMARY KEY)"))
        await connection.execute(text("""
            CREATE TABLE vpn_access_keys (
                id SERIAL PRIMARY KEY, subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id),
                endpoint_id INTEGER NULL, worker_id INTEGER NULL, external_uuid VARCHAR(128), config_uri TEXT,
                display_name VARCHAR(64), status VARCHAR(32), expires_at TIMESTAMPTZ, issued_at TIMESTAMPTZ,
                revoke_requested_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
            )
        """))
        await connection.execute(text("INSERT INTO vpn_subscriptions (id) VALUES (1), (2)"))
        for key_id, sub_id, status, name in (
            (11, 1, "active", " Laptop "), (12, 1, "pending_revoke", ""),
            (13, 1, "revoked", "bad\u200bname"), (21, 2, "suspended", None),
        ):
            await connection.execute(text("""
                INSERT INTO vpn_access_keys
                    (id, subscription_id, external_uuid, config_uri, display_name, status,
                     expires_at, issued_at, created_at, updated_at, revoked_at)
                VALUES (:id, :sub, :uuid, :uri, :name, :status,
                        '2028-01-01', '2026-01-01', '2026-01-01', '2026-02-01',
                        CASE WHEN CAST(:status AS VARCHAR(32)) = 'revoked' THEN '2026-02-01'::timestamptz ELSE NULL END)
            """), {"id": key_id, "sub": sub_id, "status": status, "name": name,
                   "uuid": f"00000000-0000-4000-8000-{key_id:012d}",
                   "uri": f"vless://safe-fake-{key_id}@vpn.example:443#unchanged"})
        if metadata_first:
            await connection.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_backfill_is_idempotent_preserves_keys_and_advances_sequence(postgres_schema, metadata_first):
    await legacy_schema(postgres_schema.engine, metadata_first=metadata_first)
    async with postgres_schema.engine.connect() as connection:
        before = (await connection.execute(text("SELECT row_to_json(k)::text FROM vpn_access_keys k ORDER BY id"))).scalars().all()
    await migrate(postgres_schema.engine)
    async with postgres_schema.engine.begin() as connection:
        rows = (await connection.execute(text("SELECT id, device_id FROM vpn_access_keys ORDER BY id"))).all()
        assert rows == [(11, 11), (12, 12), (13, 13), (21, 21)]
        snapshot = (await connection.execute(text("SELECT row_to_json(d)::text FROM vpn_devices d ORDER BY id"))).scalars().all()
        after = (await connection.execute(text("SELECT (to_jsonb(k) - 'device_id')::text FROM vpn_access_keys k ORDER BY id"))).scalars().all()
        # Compare normalized JSON because jsonb orders keys differently from row_to_json.
        import json
        assert [json.loads(value) for value in before] == [json.loads(value) for value in after]
        devices = (await connection.execute(text("SELECT id, display_name, revoke_requested_at FROM vpn_devices ORDER BY id"))).all()
        assert [(row.id, row.display_name, row.revoke_requested_at is not None) for row in devices] == [
            (11, "Laptop", False), (12, "Профиль 12", True), (13, "Профиль 13", True), (21, "Профиль 21", False),
        ]
        await connection.execute(text("SELECT setval(pg_get_serial_sequence('vpn_devices','id'), 100, true)"))
    await migrate(postgres_schema.engine)
    async with postgres_schema.engine.begin() as connection:
        assert (await connection.execute(text("SELECT row_to_json(d)::text FROM vpn_devices d ORDER BY id"))).scalars().all() == snapshot
        assert (await connection.execute(text("SELECT (to_jsonb(k) - 'device_id')::text FROM vpn_access_keys k ORDER BY id"))).scalars().all() == after
        assert await connection.scalar(text("INSERT INTO vpn_devices (subscription_id, display_name) VALUES (1, 'New') RETURNING id")) > 100
        for statement, constraint in (
            ("UPDATE vpn_access_keys SET device_id=21 WHERE id=11", "fk_vpn_access_key_device_subscription"),
            ("UPDATE vpn_devices SET token_generation=-1 WHERE id=11", "ck_vpn_device_token_generation"),
            ("INSERT INTO vpn_access_keys (id,subscription_id,device_id,endpoint_id) VALUES (50,1,11,7), (51,1,11,7)", "uq_vpn_access_key_device_endpoint"),
        ):
            async with connection.begin_nested() as savepoint:
                with pytest.raises(IntegrityError) as error:
                    await connection.execute(text(statement))
                assert constraint in str(error.value)
                await savepoint.rollback()
        await connection.execute(text("INSERT INTO vpn_access_keys (id,subscription_id,device_id,endpoint_id) VALUES (52,1,11,NULL), (53,1,11,NULL)"))


@pytest.mark.asyncio
async def test_backfill_identity_conflict_fails_closed(postgres_schema):
    await legacy_schema(postgres_schema.engine)
    await migrate(postgres_schema.engine)
    async with postgres_schema.engine.begin() as connection:
        await connection.execute(text("UPDATE vpn_access_keys SET device_id=NULL WHERE id=11"))
        await connection.execute(text("UPDATE vpn_devices SET legacy_profile_id=NULL WHERE id=11"))
    with pytest.raises(Exception, match="vpn_device_backfill_identity_conflict"):
        await migrate(postgres_schema.engine)
    async with postgres_schema.engine.connect() as connection:
        assert await connection.scalar(text("SELECT device_id FROM vpn_access_keys WHERE id=11")) is None


@pytest.mark.asyncio
async def test_fresh_metadata_migration_catalog_matches_model(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await migrate(postgres_schema.engine)
    await migrate(postgres_schema.engine)
    async with postgres_schema.engine.connect() as connection:
        constraints = (await connection.execute(text("""
            SELECT conname FROM pg_constraint WHERE conrelid IN ('vpn_devices'::regclass, 'vpn_access_keys'::regclass)
        """))).scalars().all()
        expected = {constraint.name for table in (models.VpnDevice.__table__, models.VpnAccessKey.__table__)
                    for constraint in table.constraints if constraint.name and ('device' in constraint.name)}
        assert expected <= set(constraints)
        assert all(constraints.count(name) == 1 for name in expected)
        indexes = set((await connection.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"))).scalars())
        assert {"ix_vpn_devices_subscription_id", "ix_vpn_access_keys_device_id"} <= indexes


@pytest.mark.asyncio
async def test_constraint_names_in_other_schema_do_not_suppress_device_constraints(postgres_schema):
    await legacy_schema(postgres_schema.engine)
    decoy_schema = postgres_schema.name + "_decoy"
    async with postgres_schema.engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{decoy_schema}"'))
        await connection.execute(text(f'''CREATE TABLE "{decoy_schema}".decoy (
            value INTEGER,
            CONSTRAINT fk_vpn_access_key_device_subscription CHECK (value > 0),
            CONSTRAINT uq_vpn_access_key_device_endpoint CHECK (value < 10)
        )'''))
    try:
        await migrate(postgres_schema.engine)
        async with postgres_schema.engine.connect() as connection:
            assert set((await connection.execute(text("""
                SELECT conname FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass
                AND conname IN ('fk_vpn_access_key_device_subscription','uq_vpn_access_key_device_endpoint')
            """))).scalars()) == {"fk_vpn_access_key_device_subscription", "uq_vpn_access_key_device_endpoint"}
    finally:
        async with postgres_schema.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{decoy_schema}" CASCADE'))
