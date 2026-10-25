import pytest
from sqlalchemy import text
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)

from app.db.base import Base


def test_publication_migration_is_a_separate_registered_group():
    from app.db import vpn_device_migrations
    from app.db.migrations import MIGRATIONS, VPN_UNLIMITED_INVITATION_MIGRATIONS

    assert hasattr(vpn_device_migrations, "VPN_ENDPOINT_PUBLICATION_MIGRATIONS")
    publication = vpn_device_migrations.VPN_ENDPOINT_PUBLICATION_MIGRATIONS
    start = MIGRATIONS.index(publication[0])
    assert MIGRATIONS[start:start + len(publication)] == publication
    assert start > MIGRATIONS.index(VPN_UNLIMITED_INVITATION_MIGRATIONS[-1])
    assert not any("is_published" in statement for statement in vpn_device_migrations.VPN_DEVICE_MIGRATIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_publication_upgrade_repeats_and_preserves_credentials(postgres_schema, metadata_first):
    from app.db.vpn_device_migrations import VPN_ENDPOINT_PUBLICATION_MIGRATIONS

    async with postgres_schema.engine.begin() as connection:
        if metadata_first:
            await connection.run_sync(Base.metadata.create_all)
        else:
            await connection.execute(text("CREATE TABLE vpn_endpoints (id SERIAL PRIMARY KEY)"))
            await connection.execute(text("""CREATE TABLE vpn_access_keys (
                id SERIAL PRIMARY KEY, external_uuid TEXT, expires_at TIMESTAMPTZ, config_uri TEXT
            )"""))
            await connection.execute(text("INSERT INTO vpn_endpoints DEFAULT VALUES"))
            await connection.execute(text("""INSERT INTO vpn_access_keys (external_uuid, expires_at, config_uri)
                VALUES ('00000000-0000-4000-8000-000000000001', '2028-01-01',
                        'vless://00000000-0000-4000-8000-000000000001@vpn.example:443#unchanged')"""))
        before = (await connection.execute(text("SELECT row_to_json(k)::text FROM vpn_access_keys k ORDER BY id"))).scalars().all()

    for _ in range(2):
        async with postgres_schema.engine.begin() as connection:
            for statement in VPN_ENDPOINT_PUBLICATION_MIGRATIONS:
                await connection.execute(text(statement))
            assert (await connection.execute(text("SELECT row_to_json(k)::text FROM vpn_access_keys k ORDER BY id"))).scalars().all() == before
            assert (await connection.execute(text("SELECT is_published, public_name, country_code FROM vpn_endpoints"))).all() == (
                [] if metadata_first else [(False, None, None)])
            columns = (await connection.execute(text("""SELECT column_name, data_type, character_maximum_length,
                is_nullable, column_default FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name='vpn_endpoints'
                  AND column_name IN ('is_published','public_name','country_code') ORDER BY column_name"""))).all()
            assert [(row.column_name, row.data_type, row.character_maximum_length, row.is_nullable)
                    for row in columns] == [("country_code", "character varying", 2, "YES"),
                                           ("is_published", "boolean", None, "NO"),
                                           ("public_name", "character varying", 64, "YES")]
            assert columns[1].column_default == "false"
