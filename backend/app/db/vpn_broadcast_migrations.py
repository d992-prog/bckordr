VPN_BOT_CHAT_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS vpn_bot_chats (
        bot_id VARCHAR(64) NOT NULL,
        chat_id VARCHAR(64) NOT NULL,
        first_seen_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL,
        blocked_at TIMESTAMPTZ NULL,
        last_update_id BIGINT NULL,
        PRIMARY KEY (bot_id, chat_id),
        CONSTRAINT ck_vpn_bot_chat_update_id CHECK (last_update_id IS NULL OR last_update_id >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_bot_chats_bot_id_blocked_at ON vpn_bot_chats(bot_id, blocked_at)",
)

VPN_BROADCAST_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS vpn_broadcast_campaigns (
        id SERIAL PRIMARY KEY,
        bot_id VARCHAR(64) NOT NULL,
        body TEXT NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'draft',
        revision INTEGER NOT NULL DEFAULT 1,
        preview_revision INTEGER NULL,
        preview_recipient_count INTEGER NULL,
        previewed_at TIMESTAMPTZ NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        confirmed_at TIMESTAMPTZ NULL,
        confirmed_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        cancelled_at TIMESTAMPTZ NULL,
        cancelled_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT uq_vpn_broadcast_campaign_bot UNIQUE (id, bot_id),
        CONSTRAINT ck_vpn_broadcast_campaign_state CHECK (state IN ('draft','running','completed','cancelled','failed')),
        CONSTRAINT ck_vpn_broadcast_campaign_revision CHECK (revision > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_broadcast_deliveries (
        id SERIAL PRIMARY KEY,
        campaign_id INTEGER NOT NULL,
        bot_id VARCHAR(64) NOT NULL,
        chat_id VARCHAR(64) NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'pending',
        available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        claim_token VARCHAR(36) NULL,
        lease_until TIMESTAMPTZ NULL,
        sending_at TIMESTAMPTZ NULL,
        finished_at TIMESTAMPTZ NULL,
        telegram_message_id BIGINT NULL,
        error_code VARCHAR(64) NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        CONSTRAINT fk_vpn_broadcast_delivery_campaign FOREIGN KEY (campaign_id, bot_id) REFERENCES vpn_broadcast_campaigns(id, bot_id) ON DELETE RESTRICT,
        CONSTRAINT fk_vpn_broadcast_delivery_chat FOREIGN KEY (bot_id, chat_id) REFERENCES vpn_bot_chats(bot_id, chat_id) ON DELETE RESTRICT,
        CONSTRAINT uq_vpn_broadcast_delivery_recipient UNIQUE (campaign_id, chat_id),
        CONSTRAINT ck_vpn_broadcast_delivery_state CHECK (state IN ('pending','claimed','sending','sent','failed','skipped','uncertain','cancelled')),
        CONSTRAINT ck_vpn_broadcast_delivery_attempt_count CHECK (attempt_count >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_broadcast_deliveries_due ON vpn_broadcast_deliveries(campaign_id, state, available_at)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_broadcast_deliveries_lease ON vpn_broadcast_deliveries(state, lease_until)",
    """
    CREATE TABLE IF NOT EXISTS vpn_bot_rate_gates (
        token_fingerprint VARCHAR(64) PRIMARY KEY,
        next_send_at TIMESTAMPTZ NULL,
        cooldown_until TIMESTAMPTZ NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vpn_bot_chat_rate_gates (
        token_fingerprint VARCHAR(64) NOT NULL REFERENCES vpn_bot_rate_gates(token_fingerprint) ON DELETE RESTRICT,
        chat_id VARCHAR(64) NOT NULL,
        next_send_at TIMESTAMPTZ NULL,
        PRIMARY KEY (token_fingerprint, chat_id)
    )
    """,
)
