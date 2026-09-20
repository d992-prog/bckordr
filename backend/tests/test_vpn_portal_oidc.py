from __future__ import annotations

import asyncio
import base64
import gzip
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.vpn_portal_telegram import (
    TELEGRAM_AUTHORIZE_URL,
    TELEGRAM_ISSUER,
    TELEGRAM_JWKS_URL,
    TELEGRAM_TOKEN_URL,
    TelegramAuthenticationError,
    TelegramJWKSProvider,
    authorization_url,
    exchange_authorization_code,
    verify_id_token,
)
from app.services.vpn_telegram_identity import TelegramIdentity


CLIENT_ID = "test-client"
KID = "test-rsa-key"


@pytest.fixture(scope="module")
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return private_key, public_jwk


def oidc_claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": TELEGRAM_ISSUER,
        "aud": CLIENT_ID,
        "sub": "123456",
        "iat": now,
        "exp": now + 300,
        "id": 123456,
        "preferred_username": "client",
        "given_name": "Test",
        "family_name": "User",
    }
    claims.update(overrides)
    return claims


def signed_token(private_key, *, algorithm="RS256", headers=None, **claims):
    return jwt.encode(
        oidc_claims(**claims),
        private_key,
        algorithm=algorithm,
        headers={"kid": KID, **(headers or {})},
    )


def signed_unvalidated_claims(private_key, **overrides):
    payload = json.dumps(
        oidc_claims(**overrides),
        separators=(",", ":"),
    ).encode()
    return jwt.api_jws.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": KID},
    )


def assert_authentication_failure(callable_):
    with pytest.raises(TelegramAuthenticationError) as error:
        callable_()
    assert str(error.value) == "telegram_authentication_failed"
    assert error.value.__cause__ is None


async def assert_async_authentication_failure(awaitable):
    with pytest.raises(TelegramAuthenticationError) as error:
        await awaitable
    assert str(error.value) == "telegram_authentication_failed"
    assert error.value.__cause__ is None


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.yielded = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class FakeClock:
    def __init__(self):
        self.value = 1_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_telegram_oidc_endpoints_are_pinned_to_official_issuer():
    assert TELEGRAM_ISSUER == "https://oauth.telegram.org"
    assert TELEGRAM_AUTHORIZE_URL == "https://oauth.telegram.org/auth"
    assert TELEGRAM_TOKEN_URL == "https://oauth.telegram.org/token"
    assert TELEGRAM_JWKS_URL == "https://oauth.telegram.org/.well-known/jwks.json"


def test_authorization_url_builds_s256_pkce_request():
    url = authorization_url(
        CLIENT_ID,
        "https://portal.example.test/auth/telegram/callback",
        "state-value",
        "verifier-value",
    )

    parsed = urlparse(url)
    expected_challenge = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(b"verifier-value").digest()
    ).rstrip(b"=").decode()
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == TELEGRAM_AUTHORIZE_URL
    assert parse_qs(parsed.query) == {
        "client_id": [CLIENT_ID],
        "redirect_uri": ["https://portal.example.test/auth/telegram/callback"],
        "response_type": ["code"],
        "scope": ["openid profile"],
        "state": ["state-value"],
        "code_challenge": [expected_challenge],
        "code_challenge_method": ["S256"],
    }


def test_verify_id_token_returns_identity_from_id_claim(rsa_keys):
    private_key, public_jwk = rsa_keys
    token = signed_token(private_key)

    identity = verify_id_token(token, public_jwk, CLIENT_ID)

    assert identity == TelegramIdentity("123456", "client", "Test", "User")


def test_verify_id_token_uses_name_when_given_name_is_absent(rsa_keys):
    private_key, public_jwk = rsa_keys
    token = signed_token(
        private_key,
        given_name=None,
        name="Telegram Name",
        family_name=None,
    )

    identity = verify_id_token(token, public_jwk, CLIENT_ID)

    assert identity == TelegramIdentity("123456", "client", "Telegram Name", None)


@pytest.mark.parametrize("missing_claim", ["iss", "aud", "sub", "iat", "exp", "id"])
def test_verify_id_token_rejects_missing_required_claim(rsa_keys, missing_claim):
    private_key, public_jwk = rsa_keys
    claims = oidc_claims()
    claims.pop(missing_claim)
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": KID},
    )

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


@pytest.mark.parametrize(
    "invalid_claim",
    [
        "issuer",
        "audience",
        "expired",
        "future",
    ],
)
def test_verify_id_token_rejects_invalid_claims(rsa_keys, invalid_claim):
    private_key, public_jwk = rsa_keys
    now = int(time.time())
    overrides = {
        "issuer": {"iss": "https://attacker.example"},
        "audience": {"aud": "other-client"},
        "expired": {"exp": now - 120},
        "future": {"iat": now + 120},
    }[invalid_claim]
    token = signed_token(private_key, **overrides)

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


def test_verify_id_token_uses_id_when_subject_is_different(rsa_keys):
    private_key, public_jwk = rsa_keys
    token = signed_token(private_key, id=987654321, sub="1234123412341234123")

    identity = verify_id_token(token, public_jwk, CLIENT_ID)

    assert identity.user_id == "987654321"


def test_verify_id_token_rejects_invalid_signature(rsa_keys):
    _private_key, public_jwk = rsa_keys
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = signed_token(wrong_key)

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


def test_verify_id_token_rejects_hs256_and_none_algorithms(rsa_keys):
    _private_key, public_jwk = rsa_keys
    hs_token = jwt.encode(
        oidc_claims(),
        "test-secret-that-is-at-least-thirty-two-bytes-long",
        algorithm="HS256",
        headers={"kid": KID},
    )
    none_token = jwt.encode(
        oidc_claims(),
        key="",
        algorithm="none",
        headers={"kid": KID},
    )

    assert_authentication_failure(
        lambda: verify_id_token(hs_token, public_jwk, CLIENT_ID)
    )
    assert_authentication_failure(
        lambda: verify_id_token(none_token, public_jwk, CLIENT_ID)
    )


@pytest.mark.parametrize(
    "token_value,jwk_value,client_id",
    [
        (None, {}, CLIENT_ID),
        ("not-a-jwt", {}, CLIENT_ID),
        ("token", [], CLIENT_ID),
        ("token", {}, ""),
    ],
)
def test_verify_id_token_maps_malformed_input_to_generic_failure(
    token_value,
    jwk_value,
    client_id,
):
    assert_authentication_failure(
        lambda: verify_id_token(token_value, jwk_value, client_id)
    )


def test_verify_id_token_rejects_header_kid_mismatch(rsa_keys):
    private_key, public_jwk = rsa_keys
    token = signed_token(private_key, headers={"kid": "different-key"})

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


def test_verify_id_token_rejects_missing_header_kid(rsa_keys):
    private_key, public_jwk = rsa_keys
    token = jwt.encode(oidc_claims(), private_key, algorithm="RS256")

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


@pytest.mark.parametrize(
    "remote_header,value",
    [
        ("jku", "https://attacker.example/jwks.json"),
        ("x5u", "https://attacker.example/certificate.pem"),
        ("jwk", {"kty": "oct", "k": "attacker-controlled"}),
    ],
)
def test_verify_id_token_rejects_remote_or_embedded_key_headers(
    rsa_keys,
    remote_header,
    value,
):
    private_key, public_jwk = rsa_keys
    token = signed_token(private_key, headers={remote_header: value})

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": {"nested": "value"}},
        {"sub": 123456},
        {"iss": [TELEGRAM_ISSUER]},
        {"aud": {"nested": CLIENT_ID}},
        {"iat": [int(time.time())]},
        {"exp": {"nested": int(time.time()) + 300}},
    ],
)
def test_verify_id_token_maps_malformed_claim_types_to_generic_failure(
    rsa_keys,
    overrides,
):
    private_key, public_jwk = rsa_keys
    token = signed_unvalidated_claims(private_key, **overrides)

    assert_authentication_failure(
        lambda: verify_id_token(token, public_jwk, CLIENT_ID)
    )


@pytest.mark.asyncio
async def test_jwks_provider_accepts_mixed_algorithms_and_missing_metadata(rsa_keys):
    _private_key, public_jwk = rsa_keys
    rsa_without_metadata = {
        key: value for key, value in public_jwk.items() if key not in {"alg", "use"}
    }
    document = {
        "keys": [
            {"kty": "EC", "kid": "ec", "alg": "ES256", "use": "sig"},
            {"kty": "EC", "alg": "ES256", "use": "sig"},
            {"kty": "OKP", "kid": "okp", "alg": "EdDSA", "use": "sig"},
            {"kty": "EC", "kid": "secp", "alg": "ES256K", "use": "sig"},
            rsa_without_metadata,
        ]
    }

    async def handler(request):
        assert str(request.url) == TELEGRAM_JWKS_URL
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        selected = await provider.get_key(KID)

    assert selected == rsa_without_metadata


@pytest.mark.asyncio
async def test_jwks_provider_accepts_document_with_exactly_20_keys(rsa_keys):
    _private_key, public_jwk = rsa_keys
    keys = [
        {"kty": "EC", "kid": f"ignored-{index}", "alg": "ES256"}
        for index in range(19)
    ]
    keys.append(public_jwk)

    async def handler(_request):
        return httpx.Response(200, json={"keys": keys})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = await TelegramJWKSProvider(client).get_key(KID)

    assert selected == public_jwk


@pytest.mark.asyncio
async def test_jwks_provider_caches_hits_for_300_seconds(rsa_keys):
    _private_key, public_jwk = rsa_keys
    clock = FakeClock()
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"keys": [public_jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client, monotonic=clock)
        assert await provider.get_key(KID) == public_jwk
        clock.advance(299)
        assert await provider.get_key(KID) == public_jwk

    assert requests == 1


@pytest.mark.asyncio
async def test_jwks_provider_refreshes_after_expiry_without_stale_fallback(rsa_keys):
    _private_key, public_jwk = rsa_keys
    clock = FakeClock()
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json={"keys": [public_jwk]})
        raise httpx.ConnectError("test outage")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client, monotonic=clock)
        assert await provider.get_key(KID) == public_jwk
        clock.advance(300)
        await assert_async_authentication_failure(provider.get_key(KID))

    assert requests == 2


@pytest.mark.asyncio
async def test_jwks_provider_refreshes_once_for_rotated_unknown_kid(rsa_keys):
    _private_key, first_jwk = rsa_keys
    second_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    second_jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(second_private.public_key())
    )
    second_jwk.update({"kid": "rotated", "alg": "RS256", "use": "sig"})
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        keys = [first_jwk] if requests == 1 else [second_jwk]
        return httpx.Response(200, json={"keys": keys})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        assert await provider.get_key(KID) == first_jwk
        assert await provider.get_key("rotated") == second_jwk

    assert requests == 2


@pytest.mark.asyncio
async def test_jwks_provider_throttles_unknown_kid_refresh_even_after_outage(rsa_keys):
    _private_key, public_jwk = rsa_keys
    clock = FakeClock()
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        if requests == 2:
            raise httpx.ConnectError("test outage")
        return httpx.Response(200, json={"keys": [public_jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client, monotonic=clock)
        assert await provider.get_key(KID) == public_jwk
        await assert_async_authentication_failure(provider.get_key("unknown-one"))
        await assert_async_authentication_failure(provider.get_key("unknown-two"))
        assert requests == 2
        clock.advance(30)
        await assert_async_authentication_failure(provider.get_key("unknown-three"))

    assert requests == 3


@pytest.mark.asyncio
async def test_jwks_provider_deduplicates_concurrent_initial_fetches(rsa_keys):
    _private_key, public_jwk = rsa_keys
    requests = 0

    async def handler(_request):
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"keys": [public_jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        results = await asyncio.gather(*(provider.get_key(KID) for _ in range(10)))

    assert results == [public_jwk] * 10
    assert requests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {"keys": [{"kty": "EC", "kid": str(index)} for index in range(21)]},
        {
            "keys": [
                {"kty": "EC", "kid": "duplicate"},
                {"kty": "OKP", "kid": "duplicate"},
            ]
        },
    ],
    ids=["more-than-20-keys", "duplicate-kid"],
)
async def test_jwks_provider_rejects_key_bounds_and_duplicate_kids(document):
    async def handler(_request):
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        await assert_async_authentication_failure(provider.get_key("missing"))


@pytest.mark.asyncio
async def test_jwks_provider_stops_reading_oversized_chunked_response():
    stream = ChunkedStream([b"x" * 300_000 for _ in range(5)])

    async def handler(_request):
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        await assert_async_authentication_failure(provider.get_key("missing"))

    assert stream.yielded == 4


@pytest.mark.asyncio
async def test_jwks_provider_bounds_decompressed_response():
    decompressed = b'{' + b'"padding":"' + b"x" * 1_100_000 + b'"}'

    async def handler(_request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=ChunkedStream([gzip.compress(decompressed)]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await assert_async_authentication_failure(
            TelegramJWKSProvider(client).get_key("missing")
        )


@pytest.mark.asyncio
async def test_jwks_provider_rejects_redirect_without_following_location():
    requested_urls = []

    async def handler(request):
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://attacker.example/jwks.json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        await assert_async_authentication_failure(
            TelegramJWKSProvider(client).get_key("missing")
        )

    assert requested_urls == [TELEGRAM_JWKS_URL]


@pytest.mark.asyncio
async def test_jwks_provider_rejects_invalid_and_deeply_nested_json():
    responses = [b"not-json", b"[" * 2_000 + b"0" + b"]" * 2_000]

    async def handler(_request):
        return httpx.Response(200, content=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await assert_async_authentication_failure(
            TelegramJWKSProvider(client).get_key("missing")
        )
        await assert_async_authentication_failure(
            TelegramJWKSProvider(client).get_key("missing")
        )


@pytest.mark.asyncio
async def test_exchange_authorization_code_posts_fixed_request_and_verifies_identity(
    rsa_keys,
):
    private_key, public_jwk = rsa_keys
    token = signed_token(private_key)
    requested_urls = []

    async def handler(request):
        requested_urls.append(str(request.url))
        if str(request.url) == TELEGRAM_TOKEN_URL:
            assert request.headers["authorization"] == (
                "Basic "
                + base64.b64encode(b"test-client:test-secret").decode()
            )
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["authorization_code"],
                "code": ["authorization-code"],
                "redirect_uri": ["https://portal.example.test/callback"],
                "client_id": [CLIENT_ID],
                "code_verifier": ["verifier-value"],
            }
            return httpx.Response(
                200,
                json={"id_token": token, "access_token": "must-be-discarded"},
            )
        if str(request.url) == TELEGRAM_JWKS_URL:
            return httpx.Response(200, json={"keys": [public_jwk]})
        raise AssertionError(f"unexpected URL {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = TelegramJWKSProvider(client)
        identity = await exchange_authorization_code(
            CLIENT_ID,
            "test-secret",
            "https://portal.example.test/callback",
            "authorization-code",
            "verifier-value",
            http_client=client,
            jwks_provider=provider,
        )

    assert identity == TelegramIdentity("123456", "client", "Test", "User")
    assert requested_urls == [TELEGRAM_TOKEN_URL, TELEGRAM_JWKS_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_exchange_authorization_code_rejects_redirects(status):
    async def handler(_request):
        return httpx.Response(status, headers={"location": "https://attacker.example"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        await assert_async_authentication_failure(
            exchange_authorization_code(
                CLIENT_ID,
                "test-secret",
                "https://portal.example.test/callback",
                "authorization-code",
                "verifier-value",
                http_client=client,
                jwks_provider=TelegramJWKSProvider(client),
            )
        )


@pytest.mark.asyncio
async def test_exchange_authorization_code_bounds_decompressed_response():
    decompressed = b'{' + b'"padding":"' + b"x" * 1_100_000 + b'"}'
    stream = ChunkedStream([gzip.compress(decompressed)])

    async def handler(_request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=stream,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await assert_async_authentication_failure(
            exchange_authorization_code(
                CLIENT_ID,
                "test-secret",
                "https://portal.example.test/callback",
                "authorization-code",
                "verifier-value",
                http_client=client,
                jwks_provider=TelegramJWKSProvider(client),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: httpx.Response(200, content=b"not-json"),
        lambda: httpx.Response(200, json={"id_token": 123}),
        lambda: httpx.Response(200, json={"id_token": "not-a-jwt"}),
        lambda: httpx.Response(200, json={"id_token": "x" * 16_385}),
    ],
    ids=["invalid-json", "non-string-token", "malformed-jwt", "oversized-token"],
)
async def test_exchange_authorization_code_rejects_invalid_token_response(
    response_factory,
):
    async def handler(_request):
        return response_factory()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await assert_async_authentication_failure(
            exchange_authorization_code(
                CLIENT_ID,
                "test-secret",
                "https://portal.example.test/callback",
                "authorization-code",
                "verifier-value",
                http_client=client,
                jwks_provider=TelegramJWKSProvider(client),
            )
        )


@pytest.mark.asyncio
async def test_exchange_authorization_code_maps_network_failure_to_generic_error():
    async def handler(_request):
        raise httpx.ConnectError("sensitive network details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await assert_async_authentication_failure(
            exchange_authorization_code(
                CLIENT_ID,
                "secret-not-in-error",
                "https://portal.example.test/callback",
                "code-not-in-error",
                "verifier-not-in-error",
                http_client=client,
                jwks_provider=TelegramJWKSProvider(client),
            )
        )
