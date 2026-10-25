VPN_REMINDER_MIGRATIONS = (
    "ALTER TABLE vpn_customers ADD COLUMN IF NOT EXISTS expiry_reminders BOOLEAN NOT NULL DEFAULT true",
    """
    CREATE TABLE IF NOT EXISTS vpn_expiry_reminders (
        id SERIAL PRIMARY KEY,
        subscription_id INTEGER NOT NULL REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL,
        event_kind VARCHAR(16) NOT NULL,
        enabled_at TIMESTAMPTZ NOT NULL,
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
        CONSTRAINT uq_vpn_expiry_reminder_event UNIQUE(subscription_id, expires_at, event_kind),
        CONSTRAINT fk_vpn_expiry_reminder_chat FOREIGN KEY(bot_id, chat_id) REFERENCES vpn_bot_chats(bot_id, chat_id) ON DELETE RESTRICT,
        CONSTRAINT ck_vpn_expiry_reminder_kind CHECK(event_kind IN ('3d','1d','expired')),
        CONSTRAINT ck_vpn_expiry_reminder_state CHECK(state IN ('pending','claimed','sending','sent','failed','skipped','uncertain')),
        CONSTRAINT ck_vpn_expiry_reminder_attempt_count CHECK(attempt_count >= 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_vpn_expiry_reminders_due ON vpn_expiry_reminders(state, available_at)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_expiry_reminders_lease ON vpn_expiry_reminders(state, lease_until)",
)
