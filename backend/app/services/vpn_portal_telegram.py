from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode

import httpx
import jwt

from app.services.vpn_telegram_identity import TelegramIdentity, identity_from_user


MAX_INIT_DATA_BYTES = 16_384
MAX_INIT_DATA_FIELDS = 32
MAX_ID_TOKEN_BYTES = 16_384
MAX_JSON_RESPONSE_BYTES = 1_048_576
MAX_JWKS_KEYS = 20
MAX_KEY_ID_LENGTH = 256
JWKS_CACHE_TTL_SECONDS = 300
UNKNOWN_KEY_REFRESH_SECONDS = 30
AUTH_DATE_PATTERN = re.compile(r"[0-9]{1,12}")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")

TELEGRAM_ISSUER = "https://oauth.telegram.org"
TELEGRAM_AUTHORIZE_URL = TELEGRAM_ISSUER + "/auth"
TELEGRAM_TOKEN_URL = TELEGRAM_ISSUER + "/token"
TELEGRAM_JWKS_URL = TELEGRAM_ISSUER + "/.well-known/jwks.json"


class TelegramAuthenticationError(ValueError):
    pass


def _unverified_key_id(token: str) -> str:
    header = jwt.get_unverified_header(token)
    key_id = header.get("kid")
    if (
        header.get("alg") != "RS256"
        or not isinstance(key_id, str)
        or not key_id
        or len(key_id) > MAX_KEY_ID_LENGTH
        or any(name in header for name in ("jku", "x5u", "jwk"))
    ):
        raise ValueError("invalid_header")
    return key_id


def authorization_url(
    client_id: str,
    callback: str,
    state: str,
    verifier: str,
) -> str:
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{TELEGRAM_AUTHORIZE_URL}?{query}"


def verify_id_token(
    token: str,
    jwk: dict,
    client_id: str,
) -> TelegramIdentity:
    try:
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode()) > MAX_ID_TOKEN_BYTES
            or not isinstance(jwk, dict)
            or not isinstance(client_id, str)
            or not client_id
        ):
            raise ValueError("invalid_input")

        key_id = _unverified_key_id(token)
        if (
            jwk.get("kty") != "RSA"
            or jwk.get("kid") != key_id
            or jwk.get("alg", "RS256") != "RS256"
            or jwk.get("use", "sig") != "sig"
        ):
            raise ValueError("invalid_key")

        key = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=TELEGRAM_ISSUER,
            audience=client_id,
            leeway=30,
            options={"require": ["iss", "aud", "sub", "iat", "exp", "id"]},
        )
        identity = identity_from_user(
            {
                "id": claims["id"],
                "username": claims.get("preferred_username"),
                "first_name": claims.get("given_name") or claims.get("name"),
                "last_name": claims.get("family_name"),
            }
        )
        return identity
    except (
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        jwt.PyJWTError,
    ):
        raise TelegramAuthenticationError("telegram_authentication_failed") from None


async def _bounded_json_response(response: httpx.Response) -> object:
    if response.status_code != 200:
        raise ValueError("unexpected_status")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_JSON_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        body.extend(chunk)
    return json.loads(body)


class TelegramJWKSProvider:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http_client = http_client
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._keys: dict[str, dict] | None = None
        self._expires_at = 0.0
        self._last_unknown_refresh_at = float("-inf")

    async def get_key(self, key_id: str) -> dict:
        try:
            if (
                not isinstance(key_id, str)
                or not key_id
                or len(key_id) > MAX_KEY_ID_LENGTH
            ):
                raise ValueError("invalid_key_id")

            async with self._lock:
                now = self._monotonic()
                fetched = False
                if self._keys is None or now >= self._expires_at:
                    self._keys = await self._fetch_keys()
                    self._expires_at = self._monotonic() + JWKS_CACHE_TTL_SECONDS
                    fetched = True

                selected = self._keys.get(key_id)
                if selected is not None:
                    return selected

                if fetched:
                    self._last_unknown_refresh_at = now
                elif now - self._last_unknown_refresh_at >= UNKNOWN_KEY_REFRESH_SECONDS:
                    self._last_unknown_refresh_at = now
                    refreshed = await self._fetch_keys()
                    self._keys = refreshed
                    self._expires_at = self._monotonic() + JWKS_CACHE_TTL_SECONDS
                    selected = refreshed.get(key_id)
                    if selected is not None:
                        return selected
                raise ValueError("unknown_key")
        except (
            KeyError,
            OverflowError,
            RecursionError,
            RuntimeError,
            TypeError,
            ValueError,
            httpx.HTTPError,
        ):
            raise TelegramAuthenticationError(
                "telegram_authentication_failed"
            ) from None

    async def _fetch_keys(self) -> dict[str, dict]:
        async with self._http_client.stream(
            "GET",
            TELEGRAM_JWKS_URL,
            timeout=10.0,
            follow_redirects=False,
        ) as response:
            document = await _bounded_json_response(response)

        if not isinstance(document, dict):
            raise ValueError("invalid_jwks")
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or len(raw_keys) > MAX_JWKS_KEYS:
            raise ValueError("invalid_jwks")

        seen_key_ids: set[str] = set()
        selected_keys: dict[str, dict] = {}
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                raise ValueError("invalid_jwk")
            key_id = raw_key.get("kid")
            valid_key_id = (
                isinstance(key_id, str)
                and bool(key_id)
                and len(key_id) <= MAX_KEY_ID_LENGTH
            )
            if valid_key_id:
                if key_id in seen_key_ids:
                    raise ValueError("duplicate_key_id")
                seen_key_ids.add(key_id)

            selectable = (
                raw_key.get("kty") == "RSA"
                and raw_key.get("alg", "RS256") == "RS256"
                and raw_key.get("use", "sig") == "sig"
            )
            if selectable:
                if not valid_key_id:
                    raise ValueError("invalid_key_id")
                selected_keys[key_id] = dict(raw_key)
        return selected_keys


async def exchange_authorization_code(
    client_id: str,
    client_secret: str,
    callback: str,
    code: str,
    verifier: str,
    *,
    http_client: httpx.AsyncClient,
    jwks_provider: TelegramJWKSProvider,
) -> TelegramIdentity:
    try:
        if not all(
            isinstance(value, str) and value
            for value in (client_id, client_secret, callback, code, verifier)
        ):
            raise ValueError("invalid_input")
        async with http_client.stream(
            "POST",
            TELEGRAM_TOKEN_URL,
            auth=httpx.BasicAuth(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            timeout=10.0,
            follow_redirects=False,
        ) as response:
            document = await _bounded_json_response(response)

        if not isinstance(document, dict):
            raise ValueError("invalid_token_response")
        token = document.get("id_token")
        if (
            not isinstance(token, str)
            or not token
            or len(token.encode()) > MAX_ID_TOKEN_BYTES
        ):
            raise ValueError("invalid_id_token")
        del document

        key_id = _unverified_key_id(token)
        jwk = await jwks_provider.get_key(key_id)
        return verify_id_token(token, jwk, client_id)
    except (
        KeyError,
        OverflowError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
        jwt.PyJWTError,
        httpx.HTTPError,
    ):
        raise TelegramAuthenticationError("telegram_authentication_failed") from None


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
