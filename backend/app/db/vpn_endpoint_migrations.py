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
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_endpoint_worker_inbound UNIQUE (worker_id, inbound_id),
        CONSTRAINT uq_vpn_endpoint_id_worker UNIQUE (id, worker_id),
        CONSTRAINT ck_vpn_endpoint_inbound CHECK (inbound_id > 0),
        CONSTRAINT ck_vpn_endpoint_port CHECK (port BETWEEN 1 AND 65535),
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
)
