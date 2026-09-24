VPN_ENDPOINT_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS vpn_endpoints (
        id SERIAL PRIMARY KEY,
        worker_id INTEGER NOT NULL REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        inbound_id INTEGER NOT NULL,
        public_host VARCHAR(255) NOT NULL,
        port INTEGER NOT NULL,
        protocol VARCHAR(32) NOT NULL DEFAULT 'vless',
        transport VARCHAR(32) NOT NULL DEFAULT 'tcp',
        security VARCHAR(32) NOT NULL DEFAULT 'none',
        server_name VARCHAR(255) NULL,
        public_key VARCHAR(128) NULL,
        short_id VARCHAR(16) NULL,
        fingerprint VARCHAR(32) NULL,
        flow VARCHAR(32) NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'staged',
        verified_at TIMESTAMPTZ NULL,
        last_error_code VARCHAR(64) NULL,
        max_active_profiles INTEGER NULL,
        capacity_warning_percent INTEGER NOT NULL DEFAULT 80,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_endpoint_worker_inbound UNIQUE (worker_id, inbound_id),
        CONSTRAINT uq_vpn_endpoint_id_worker UNIQUE (id, worker_id),
        CONSTRAINT ck_vpn_endpoint_inbound CHECK (inbound_id > 0),
        CONSTRAINT ck_vpn_endpoint_port CHECK (port BETWEEN 1 AND 65535),
        CONSTRAINT ck_vpn_endpoint_capacity CHECK (
            max_active_profiles IS NULL OR max_active_profiles > 0
        ),
        CONSTRAINT ck_vpn_endpoint_capacity_warning CHECK (
            capacity_warning_percent BETWEEN 1 AND 100
        ),
        CONSTRAINT ck_vpn_endpoint_status CHECK (status IN ('staged','ready','draining','disabled')),
        CONSTRAINT ck_vpn_endpoint_security CHECK (security IN ('none','tls','reality')),
        CONSTRAINT ck_vpn_endpoint_ready CHECK (
            status <> 'ready' OR (security IN ('tls','reality') AND verified_at IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_endpoints_worker_id ON vpn_endpoints(worker_id)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_endpoints_status ON vpn_endpoints(status)",
    "ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS endpoint_id INTEGER NULL",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass AND conname='fk_vpn_access_key_endpoint_worker') THEN
            ALTER TABLE vpn_access_keys
                ADD CONSTRAINT fk_vpn_access_key_endpoint_worker
                FOREIGN KEY (endpoint_id, worker_id)
                REFERENCES vpn_endpoints(id, worker_id)
                ON DELETE RESTRICT;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass AND conname='ck_vpn_access_key_endpoint_worker') THEN
            ALTER TABLE vpn_access_keys
                ADD CONSTRAINT ck_vpn_access_key_endpoint_worker
                CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL);
        END IF;
    END;
    $$
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_endpoint_id ON vpn_access_keys(endpoint_id)",
    """
    ALTER TABLE vpn_access_keys
        ADD COLUMN IF NOT EXISTS operation_generation INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE vpn_access_keys
        ADD COLUMN IF NOT EXISTS revoke_requested_at TIMESTAMPTZ NULL
    """,
    """
    ALTER TABLE vpn_access_keys
        ADD COLUMN IF NOT EXISTS verified_client_email VARCHAR(64) NULL
    """,
    """
    ALTER TABLE vpn_access_keys
        ADD COLUMN IF NOT EXISTS panel_sub_id VARCHAR(64) NULL
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass AND conname='ck_vpn_access_key_operation_generation') THEN
            ALTER TABLE vpn_access_keys
                ADD CONSTRAINT ck_vpn_access_key_operation_generation
                CHECK (operation_generation >= 0);
        END IF;
    END;
    $$
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_control_operations (
        id VARCHAR(36) PRIMARY KEY,
        access_key_id INTEGER NOT NULL REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        worker_id INTEGER NOT NULL,
        endpoint_id INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        action VARCHAR(16) NOT NULL,
        request_snapshot JSON NOT NULL,
        request_digest VARCHAR(64) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'queued',
        claim_token VARCHAR(36) NULL,
        claimed_at TIMESTAMPTZ NULL,
        finished_at TIMESTAMPTZ NULL,
        error_code VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_control_operation_key_generation
            UNIQUE (access_key_id, generation),
        CONSTRAINT fk_vpn_control_operation_endpoint_worker
            FOREIGN KEY (endpoint_id, worker_id)
            REFERENCES vpn_endpoints(id, worker_id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_vpn_control_operation_generation CHECK (generation > 0),
        CONSTRAINT ck_vpn_control_operation_action
            CHECK (action IN ('provision','suspend','revoke')),
        CONSTRAINT ck_vpn_control_operation_state
            CHECK (state IN ('queued','claimed','uncertain','succeeded','failed','superseded'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_state
    ON vpn_control_operations(state)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_worker_id
    ON vpn_control_operations(worker_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_access_key_id
    ON vpn_control_operations(access_key_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_control_operations_claim_token
    ON vpn_control_operations(claim_token)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_control_operations_worker_reserved
    ON vpn_control_operations(worker_id)
    WHERE state IN ('claimed','uncertain')
    """,
    "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS max_active_profiles INTEGER NULL",
    "ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS capacity_warning_percent INTEGER NOT NULL DEFAULT 80",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_endpoints'::regclass AND conname='ck_vpn_endpoint_capacity') THEN
            ALTER TABLE vpn_endpoints ADD CONSTRAINT ck_vpn_endpoint_capacity
                CHECK (max_active_profiles IS NULL OR max_active_profiles > 0);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='vpn_endpoints'::regclass AND conname='ck_vpn_endpoint_capacity_warning') THEN
            ALTER TABLE vpn_endpoints ADD CONSTRAINT ck_vpn_endpoint_capacity_warning
                CHECK (capacity_warning_percent BETWEEN 1 AND 100);
        END IF;
    END;
    $$
    """,
)
