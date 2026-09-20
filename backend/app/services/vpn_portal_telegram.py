from __future__ import annotations

import hashlib
import hmac
import json
import re
from urllib.parse import parse_qsl

from app.services.vpn_telegram_identity import TelegramIdentity, identity_from_user


MAX_INIT_DATA_BYTES = 16_384
MAX_INIT_DATA_FIELDS = 32
AUTH_DATE_PATTERN = re.compile(r"[0-9]{1,12}")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class TelegramAuthenticationError(ValueError):
    pass


def verify_mini_app(
    raw: str,
    bot_token: str,
    now_seconds: int,
) -> tuple[TelegramIdentity, str, int]:
    try:
        if (
            not isinstance(raw, str)
            or not isinstance(bot_token, str)
            or not raw
            or not bot_token
            or len(raw.encode()) > MAX_INIT_DATA_BYTES
        ):
            raise ValueError("invalid_input")

        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            max_num_fields=MAX_INIT_DATA_FIELDS,
        )
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate_fields")

        received_hash = fields.pop("hash")
        if HASH_PATTERN.fullmatch(received_hash) is None:
            raise ValueError("invalid_hash")

        data_check_string = "\n".join(
            f"{key}={fields[key]}" for key in sorted(fields)
        )
        secret = hmac.new(
            b"WebAppData",
            bot_token.encode(),
            hashlib.sha256,
        ).digest()
        expected_hash = hmac.new(
            secret,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            raise ValueError("invalid_signature")

        raw_timestamp = fields["auth_date"]
        if AUTH_DATE_PATTERN.fullmatch(raw_timestamp) is None:
            raise ValueError("invalid_timestamp")
        timestamp = int(raw_timestamp)
        if not now_seconds - 300 <= timestamp <= now_seconds + 30:
            raise ValueError("expired")

        user = json.loads(fields["user"])
        if not isinstance(user, dict):
            raise ValueError("invalid_user")
        identity = identity_from_user(user)
        digest = hashlib.sha256(
            (data_check_string + "\n" + received_hash).encode()
        ).hexdigest()
        return identity, digest, timestamp
    except (KeyError, OverflowError, RecursionError, TypeError, ValueError):
        raise TelegramAuthenticationError("telegram_authentication_failed") from None
