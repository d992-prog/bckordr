VPN_DEVICE_OPERATIONS_MIGRATIONS = (
    "ALTER TABLE vpn_devices ADD COLUMN IF NOT EXISTS credential_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS credential_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE vpn_access_keys DROP CONSTRAINT IF EXISTS uq_vpn_access_key_device_endpoint",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_devices'::regclass
                       AND conname='ck_vpn_device_credential_generation') THEN
            ALTER TABLE vpn_devices ADD CONSTRAINT ck_vpn_device_credential_generation
                CHECK (credential_generation >= 0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass
                       AND conname='ck_vpn_access_key_credential_generation') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT ck_vpn_access_key_credential_generation
                CHECK (credential_generation >= 0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass
                       AND conname='uq_vpn_access_key_device_generation_endpoint') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT uq_vpn_access_key_device_generation_endpoint
                UNIQUE (device_id, credential_generation, endpoint_id);
        END IF;
    END;
    $$
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_device_operations (
        id VARCHAR(36) PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES vpn_customers(id) ON DELETE RESTRICT,
        request_id VARCHAR(36) NOT NULL,
        device_id INTEGER NOT NULL REFERENCES vpn_devices(id) ON DELETE RESTRICT,
        action VARCHAR(16) NOT NULL,
        stage VARCHAR(16) NOT NULL,
        credential_generation INTEGER NOT NULL,
        endpoint_id INTEGER NULL REFERENCES vpn_endpoints(id) ON DELETE RESTRICT,
        request_identity JSON NOT NULL,
        public_error VARCHAR(64) NULL,
        finished_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_device_operation_request UNIQUE (customer_id, request_id),
        CONSTRAINT ck_vpn_device_operation_generation CHECK (credential_generation >= 0),
        CONSTRAINT ck_vpn_device_operation_action CHECK (action IN ('create','revoke','replace')),
        CONSTRAINT ck_vpn_device_operation_stage
            CHECK (stage IN ('revoking','provisioning','completed','failed','uncertain'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_device_operations_customer_id ON vpn_device_operations(customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_device_operations_device_id ON vpn_device_operations(device_id)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_device_operations_stage ON vpn_device_operations(stage)",
)
