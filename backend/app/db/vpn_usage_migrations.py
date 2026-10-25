"""Additive usage and private source-evidence schema; no historical backfill."""

VPN_USAGE_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS vpn_node_usage_polls (
        worker_id INTEGER PRIMARY KEY REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        claim_token VARCHAR(36) NULL,
        lease_until TIMESTAMPTZ NULL,
        next_poll_at TIMESTAMPTZ NULL,
        last_attempt_at TIMESTAMPTZ NULL,
        last_success_at TIMESTAMPTZ NULL,
        last_error_code VARCHAR(64) NULL,
        traffic_state VARCHAR(16) NOT NULL DEFAULT 'unavailable',
        source_state VARCHAR(16) NOT NULL DEFAULT 'unavailable',
        protocol_version INTEGER NULL,
        CONSTRAINT ck_vpn_usage_poll_traffic CHECK (traffic_state IN ('ok','partial','unavailable')),
        CONSTRAINT ck_vpn_usage_poll_source CHECK (source_state IN ('ok','unsupported','unavailable')),
        CONSTRAINT ck_vpn_usage_poll_protocol CHECK (protocol_version IS NULL OR protocol_version = 1),
        CONSTRAINT ck_vpn_usage_poll_claim CHECK ((claim_token IS NULL) = (lease_until IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_key_usage_samples (
        access_key_id INTEGER PRIMARY KEY REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        worker_id INTEGER NOT NULL REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        sampled_at TIMESTAMPTZ NOT NULL,
        counter_epoch VARCHAR(64) NOT NULL,
        record_id BIGINT NOT NULL,
        reset_count BIGINT NOT NULL,
        upload_bytes BIGINT NOT NULL,
        download_bytes BIGINT NOT NULL,
        CONSTRAINT ck_vpn_usage_sample_record CHECK (record_id BETWEEN 1 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_sample_reset CHECK (reset_count BETWEEN 0 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_sample_upload CHECK (upload_bytes BETWEEN 0 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_sample_download CHECK (download_bytes BETWEEN 0 AND 9223372036854775807)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_usage_hours (
        access_key_id INTEGER NOT NULL REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        worker_id INTEGER NOT NULL REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        hour_start TIMESTAMPTZ NOT NULL,
        upload_bytes BIGINT NOT NULL,
        download_bytes BIGINT NOT NULL,
        sample_count BIGINT NOT NULL,
        first_sample_at TIMESTAMPTZ NOT NULL,
        last_sample_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (access_key_id, worker_id, hour_start),
        CONSTRAINT ck_vpn_usage_hour_upload CHECK (upload_bytes BETWEEN 0 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_hour_download CHECK (download_bytes BETWEEN 0 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_hour_count CHECK (sample_count BETWEEN 1 AND 9223372036854775807),
        CONSTRAINT ck_vpn_usage_hour_interval CHECK (last_sample_at > first_sample_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_usage_hours_hour_start ON vpn_usage_hours(hour_start)",
    """
    CREATE TABLE IF NOT EXISTS vpn_key_source_samples (
        access_key_id INTEGER PRIMARY KEY REFERENCES vpn_access_keys(id) ON DELETE RESTRICT,
        worker_id INTEGER NOT NULL REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        minute_at TIMESTAMPTZ NOT NULL,
        observed_at TIMESTAMPTZ NOT NULL,
        state VARCHAR(16) NOT NULL,
        key_epoch VARCHAR(16) NULL,
        source_hashes JSON NULL,
        CONSTRAINT ck_vpn_source_sample_state CHECK (state IN ('ok','unsupported','unavailable'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_key_source_samples_observed_at ON vpn_key_source_samples(observed_at)",
    """
    CREATE TABLE IF NOT EXISTS vpn_device_abuse_state (
        device_id INTEGER PRIMARY KEY REFERENCES vpn_devices(id) ON DELETE RESTRICT,
        last_minute_at TIMESTAMPTZ NULL,
        started_at TIMESTAMPTZ NULL,
        consecutive_windows INTEGER NOT NULL DEFAULT 0,
        last_source_count INTEGER NULL,
        coverage VARCHAR(16) NOT NULL DEFAULT 'unavailable',
        key_epoch VARCHAR(16) NULL,
        CONSTRAINT ck_vpn_abuse_state_windows CHECK (consecutive_windows BETWEEN 0 AND 5),
        CONSTRAINT ck_vpn_abuse_state_count CHECK (last_source_count IS NULL OR last_source_count >= 0),
        CONSTRAINT ck_vpn_abuse_state_coverage CHECK (coverage IN ('ok','collecting','unsupported','unavailable'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_abuse_incidents (
        id SERIAL PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES vpn_devices(id) ON DELETE RESTRICT,
        started_at TIMESTAMPTZ NOT NULL,
        last_observed_at TIMESTAMPTZ NOT NULL,
        peak_source_count INTEGER NOT NULL,
        acknowledged_at TIMESTAMPTZ NULL,
        acknowledged_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE RESTRICT,
        CONSTRAINT ck_vpn_abuse_incident_peak CHECK (peak_source_count > 2)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_abuse_incidents_open_device ON vpn_abuse_incidents(device_id) WHERE acknowledged_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_vpn_abuse_incidents_last_observed_at ON vpn_abuse_incidents(last_observed_at)",
)
