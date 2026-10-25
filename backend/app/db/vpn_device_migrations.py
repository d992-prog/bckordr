VPN_DEVICE_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS vpn_devices (
        id SERIAL PRIMARY KEY,
        subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
        legacy_profile_id INTEGER NULL,
        display_name VARCHAR(64) NOT NULL,
        preferred_key_id INTEGER NULL,
        pending_key_id INTEGER NULL,
        token_generation INTEGER NOT NULL DEFAULT 0,
        revoke_requested_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_device_id_subscription UNIQUE (id, subscription_id),
        CONSTRAINT uq_vpn_device_legacy_profile UNIQUE (legacy_profile_id),
        CONSTRAINT ck_vpn_device_token_generation CHECK (token_generation >= 0),
        CONSTRAINT fk_vpn_device_legacy_profile FOREIGN KEY (legacy_profile_id)
            REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        CONSTRAINT fk_vpn_device_preferred_key FOREIGN KEY (preferred_key_id)
            REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        CONSTRAINT fk_vpn_device_pending_key FOREIGN KEY (pending_key_id)
            REFERENCES vpn_access_keys(id) ON DELETE RESTRICT
    )
    """,
    "ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS device_id INTEGER NULL",
    "CREATE INDEX IF NOT EXISTS ix_vpn_devices_subscription_id ON vpn_devices(subscription_id)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_device_id ON vpn_access_keys(device_id)",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass
                       AND conname='fk_vpn_access_key_device_subscription') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT fk_vpn_access_key_device_subscription
                FOREIGN KEY (device_id, subscription_id)
                REFERENCES vpn_devices(id, subscription_id) ON DELETE RESTRICT;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass
                       AND conname='uq_vpn_access_key_device_endpoint') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT uq_vpn_access_key_device_endpoint
                UNIQUE (device_id, endpoint_id);
        END IF;
    END;
    $$
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM vpn_access_keys k JOIN vpn_devices d ON d.id=k.id
            WHERE k.device_id IS NULL AND d.legacy_profile_id IS DISTINCT FROM k.id
        ) THEN
            RAISE EXCEPTION 'vpn_device_backfill_identity_conflict';
        END IF;
    END;
    $$
    """,
    r"""
    INSERT INTO vpn_devices (
        id, subscription_id, legacy_profile_id, display_name, preferred_key_id, pending_key_id,
        token_generation, revoke_requested_at, created_at, updated_at
    )
    SELECT k.id, k.subscription_id, k.id,
        CASE WHEN length(btrim(k.display_name)) BETWEEN 1 AND 64
            AND k.display_name !~ '[[:cntrl:]]'
            AND k.display_name !~ U&'[\00AD\0600-\0605\061C\06DD\070F\0890-\0891\08E2\180E\200B-\200F\202A-\202E\2060-\2064\2066-\206F\FEFF\FFF9-\FFFB\+0110BD\+0110CD\+013430-\+01343F\+01BCA0-\+01BCA3\+01D173-\+01D17A\+0E0001\+0E0020-\+0E007F]'
            THEN btrim(k.display_name) ELSE 'Профиль ' || k.id END,
        k.id, NULL, 0,
        CASE WHEN k.status IN ('pending_revoke', 'revoked')
            THEN COALESCE(k.revoke_requested_at, k.revoked_at, k.created_at, NOW()) ELSE NULL END,
        COALESCE(k.created_at, NOW()), COALESCE(k.updated_at, k.created_at, NOW())
    FROM vpn_access_keys k WHERE k.device_id IS NULL
    ON CONFLICT (legacy_profile_id) DO NOTHING
    """,
    """
    UPDATE vpn_access_keys k SET device_id=d.id FROM vpn_devices d
    WHERE k.device_id IS NULL AND d.legacy_profile_id=k.id AND d.subscription_id=k.subscription_id
    """,
    """
    DO $$
    DECLARE
        device_sequence TEXT;
        sequence_value BIGINT;
        highest_id BIGINT;
    BEGIN
        LOCK TABLE vpn_devices IN ACCESS EXCLUSIVE MODE;
        device_sequence := pg_get_serial_sequence('vpn_devices', 'id');
        EXECUTE format('SELECT last_value FROM %s', device_sequence) INTO sequence_value;
        SELECT COALESCE(MAX(id), 0) INTO highest_id FROM vpn_devices;
        PERFORM setval(device_sequence::regclass, GREATEST(sequence_value, highest_id, 1), true);
    END;
    $$
    """,
)


VPN_ENDPOINT_PUBLICATION_MIGRATIONS = (
    "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS is_published BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS public_name VARCHAR(64) NULL",
    "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS country_code VARCHAR(2) NULL",
)
