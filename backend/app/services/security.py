from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from app.db.base import utcnow

PASSWORD_ROUNDS = 310_000
SESSION_COOKIE_NAME = "frdm_session"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, rounds_text, salt, digest = stored_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    recalculated = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(rounds_text),
    ).hex()
    return hmac.compare_digest(recalculated, digest)


def generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_session_expiry(remember_me: bool) -> timedelta:
    return timedelta(days=30 if remember_me else 1)


def user_has_feature_access(user) -> bool:
    if user.deleted_at is not None:
        return False
    if user.role in {"owner", "admin"}:
        return True
    if user.status != "approved":
        return False
    if user.access_expires_at is None:
        return False
    return user.access_expires_at > utcnow()
