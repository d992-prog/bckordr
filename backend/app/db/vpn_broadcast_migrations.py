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
