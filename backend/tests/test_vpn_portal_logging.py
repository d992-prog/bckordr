from __future__ import annotations

import io
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from uvicorn.logging import AccessFormatter

from app.core.config import Settings
from app.db.models import VpnPortalLoginAttempt
from app.db.session import engine as production_engine
from app.services import vpn_portal_http as portal_http_module
from app.services.vpn_portal_http import PortalHttpMiddleware
from app.services.vpn_portal_logging import SafeLogFilter, install_safe_logging


PORTAL_PREFIX = "/api/vpn-portal"


def _formatted_log(
    logger_name: str,
    message: str,
    *args: object,
    exc_info: BaseException | None = None,
    stack_info: bool = False,
) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(SafeLogFilter())
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers.append(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        if exc_info is None:
            logger.error(message, *args)
        else:
            try:
                raise exc_info
            except type(exc_info):
                logger.exception(message, *args, stack_info=stack_info)
    finally:
        logger.handlers.remove(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    return stream.getvalue()


def test_safe_log_filter_redacts_portal_and_provider_credentials() -> None:
    rendered = _formatted_log(
        "httpx",
        (
            "provider uri=%s Authorization: Bearer %s Cookie: %s "
            "connection=%s"
        ),
        (
            "https://api.telegram.org/botBOT-SECRET/getMe"
            "?code=OAUTH-CODE&state=OAUTH-STATE&initData=INIT-DATA"
        ),
        "ACCESS-TOKEN",
        "veltrix_session=SESSION-COOKIE",
        "vless://uuid@example.test:443?security=reality",
        exc_info=RuntimeError("provider rejected client_secret=OIDC-SECRET"),
    )

    for secret in (
        "BOT-SECRET",
        "OAUTH-CODE",
        "OAUTH-STATE",
        "INIT-DATA",
        "ACCESS-TOKEN",
        "SESSION-COOKIE",
        "uuid@example.test",
        "OIDC-SECRET",
    ):
        assert secret not in rendered
    assert "ERROR httpx" in rendered
    assert "exception_type=RuntimeError" in rendered
    assert "[REDACTED]" in rendered


def test_safe_log_filter_redacts_complete_quoted_header_values() -> None:
    rendered = _formatted_log(
        "httpx",
        "request failed headers=%r",
        {
            "Cookie": "theme=light; veltrix_session=SESSION-MARKER",
            "Authorization": "Basic BASIC-MARKER with-spaces",
            "Proxy-Authorization": "Custom CUSTOM-MARKER",
            "Set-Cookie": "veltrix_binding=BINDING-MARKER; HttpOnly; Secure",
        },
        exc_info=RuntimeError("provider unavailable"),
    )

    for sensitive_value in (
        "theme=light",
        "SESSION-MARKER",
        "Basic",
        "BASIC-MARKER",
        "CUSTOM-MARKER",
        "BINDING-MARKER",
        "HttpOnly",
        "Secure",
    ):
        assert sensitive_value not in rendered
    assert rendered.count("[REDACTED]") == 1
    assert "request failed headers=[REDACTED]" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_redacts_json_and_bytes_header_representations() -> None:
    json_rendered = _formatted_log(
        "httpcore.http11",
        (
            r'request headers={"Cookie":"locale=en; session=JSON-COOKIE",'
            r'"Authorization":"Digest username=\"JSON-USER\", response=\"JSON-HASH\""}'
        ),
    )
    bytes_rendered = _formatted_log(
        "httpcore.http11",
        "request headers=%r",
        {
            b"cookie": b"session=BYTES-COOKIE; theme=dark",
            b"authorization": b"Negotiate BYTES-AUTH DATA",
        },
    )

    for marker in (
        "JSON-COOKIE",
        "JSON-USER",
        "JSON-HASH",
        "BYTES-COOKIE",
        "BYTES-AUTH",
        "theme=dark",
    ):
        assert marker not in json_rendered + bytes_rendered
    assert "request headers=" in json_rendered
    assert "request headers=" in bytes_rendered


@pytest.mark.parametrize(
    ("representation", "secret"),
    [
        ("headers={'Authorization': 'Basic UNTERMINATED SECRET WITH SPACES", "UNTERMINATED"),
        ("headers=[(b'authorization', b'Basic TUPLE SECRET WITH SPACES')]", "TUPLE"),
        ("headers=[('cookie', 'session=TUPLE-COOKIE; theme=light')]", "TUPLE-COOKIE"),
    ],
)
def test_safe_log_filter_fails_closed_for_malformed_and_tuple_headers(
    representation: str, secret: str
) -> None:
    rendered = _formatted_log(
        "httpx",
        representation,
        exc_info=RuntimeError("provider unavailable"),
    )

    assert secret not in rendered
    assert "headers=[REDACTED]" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_sanitizes_credentials_before_sensitive_headers() -> None:
    rendered = _formatted_log(
        "httpx",
        (
            "provider failed code=%s state=%s client_secret=%s "
            "token Bearer %s headers=%r"
        ),
        "CODE-MARKER",
        "STATE-MARKER",
        "CLIENT-SECRET",
        "PREFIX-TOKEN",
        {"Cookie": "session=COOKIE-MARKER"},
        exc_info=RuntimeError("provider unavailable"),
    )

    for marker in (
        "CODE-MARKER",
        "STATE-MARKER",
        "CLIENT-SECRET",
        "PREFIX-TOKEN",
        "COOKIE-MARKER",
    ):
        assert marker not in rendered
    assert "provider failed" in rendered
    assert "headers=[REDACTED]" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_does_not_match_header_name_suffixes() -> None:
    rendered = _formatted_log(
        "app.services.control_runtime",
        "diagnostic values=%r",
        {
            "NotCookie": "ordinary cookie diagnostic",
            "Reauthorization": "ordinary authorization diagnostic",
        },
    )

    assert "ordinary cookie diagnostic" in rendered
    assert "ordinary authorization diagnostic" in rendered
    assert "[REDACTED]" not in rendered


def test_safe_log_filter_is_idempotent_for_exception_observability() -> None:
    try:
        raise RuntimeError("provider client_secret=DOUBLE-FILTER-SECRET")
    except RuntimeError:
        record = logging.getLogger("httpx").makeRecord(
            "httpx",
            logging.ERROR,
            __file__,
            1,
            "Authorization: Bearer DOUBLE-FILTER-TOKEN",
            (),
            exc_info=sys.exc_info(),
        )

    first = SafeLogFilter()
    second = SafeLogFilter()
    assert first.filter(record) is True
    assert second.filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert "DOUBLE-FILTER-SECRET" not in rendered
    assert "DOUBLE-FILTER-TOKEN" not in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_hides_sqlalchemy_portal_auth_parameters() -> None:
    rendered = _formatted_log(
        "sqlalchemy.engine.Engine",
        "INSERT INTO vpn_portal_login_attempts (state_hash, verifier) VALUES (%s, %s) %r",
        "STATE-HASH",
        "PKCE-VERIFIER",
        ("STATE-HASH", "PKCE-VERIFIER"),
        exc_info=RuntimeError("flush failed with PKCE-VERIFIER"),
    )

    assert "STATE-HASH" not in rendered
    assert "PKCE-VERIFIER" not in rendered
    assert "portal authentication SQL [REDACTED]" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_production_database_engine_hides_bound_parameters() -> None:
    assert production_engine.sync_engine.hide_parameters is True


@pytest.mark.asyncio
async def test_sqlalchemy_real_flush_hides_pkce_parameters_but_keeps_diagnostics() -> None:
    logger = logging.getLogger("sqlalchemy.engine.Engine.portal_safety_test")
    previous_level = logger.level
    previous_propagate = logger.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(SafeLogFilter())
    logger.handlers.append(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        hide_parameters=True,
        logging_name="portal_safety_test",
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    verifier = "PKCE-VERIFIER-MARKER-0123456789abcdefghijklm"
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(VpnPortalLoginAttempt.__table__.create)
        async with factory() as session:
            session.add(
                VpnPortalLoginAttempt(
                    state_hash="state-hash",
                    binding_hash="binding-hash",
                    code_verifier=verifier,
                    created_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            await session.flush()
            session.add(
                VpnPortalLoginAttempt(
                    state_hash="state-hash",
                    binding_hash="other-binding-hash",
                    code_verifier=verifier,
                    created_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                logger.exception("portal authentication flush failed")
                await session.rollback()
    finally:
        await engine.dispose()
        logger.handlers.remove(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    rendered = stream.getvalue()
    assert verifier not in rendered
    assert "INSERT INTO vpn_portal_login_attempts" in rendered
    assert "SQL parameters hidden due to hide_parameters=True" in rendered
    assert "portal authentication flush failed exception_type=IntegrityError" in rendered


def test_safe_log_filter_preserves_ordinary_nonportal_error_details() -> None:
    rendered = _formatted_log(
        "app.services.control_runtime",
        "control cycle failed",
        exc_info=RuntimeError("ordinary diagnostic"),
        stack_info=True,
    )

    assert "control cycle failed" in rendered
    assert "RuntimeError: ordinary diagnostic" in rendered
    assert "Stack (most recent call last)" in rendered


def test_safe_log_filter_removes_secrets_from_nonportal_exception_text() -> None:
    rendered = _formatted_log(
        "app.services.notifier",
        "Telegram notification failed",
        exc_info=RuntimeError(
            "POST https://api.telegram.org/botEXCEPTION-BOT-SECRET/sendMessage failed"
        ),
    )

    assert "Telegram notification failed" in rendered
    assert "EXCEPTION-BOT-SECRET" not in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_removes_secrets_from_explicit_exception_cause() -> None:
    chained: RuntimeError | None = None
    try:
        try:
            raise RuntimeError(
                "https://api.telegram.org/botCHAIN-BOT-SECRET/sendMessage"
            )
        except RuntimeError as cause:
            raise RuntimeError("delivery failed") from cause
    except RuntimeError as error:
        chained = error
    assert chained is not None

    rendered = _formatted_log(
        "app.services.notifier",
        "Telegram notification failed",
        exc_info=chained,
    )

    assert "CHAIN-BOT-SECRET" not in rendered
    assert "Telegram notification failed" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_removes_secrets_from_implicit_exception_context() -> None:
    contextual: RuntimeError | None = None
    try:
        try:
            raise RuntimeError("client_secret=CONTEXT-CLIENT-SECRET")
        except RuntimeError:
            raise RuntimeError("delivery failed")
    except RuntimeError as error:
        contextual = error
    assert contextual is not None

    rendered = _formatted_log(
        "app.services.notifier",
        "Telegram notification failed",
        exc_info=contextual,
    )

    assert "CONTEXT-CLIENT-SECRET" not in rendered
    assert "Telegram notification failed" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_removes_secrets_from_exception_notes() -> None:
    failure = RuntimeError("ordinary delivery failure")
    failure.add_note("Authorization: Bearer NOTE-SECRET")

    rendered = _formatted_log(
        "app.services.notifier",
        "Telegram notification failed",
        exc_info=failure,
    )

    assert "NOTE-SECRET" not in rendered
    assert "Telegram notification failed" in rendered
    assert "exception_type=RuntimeError" in rendered


def test_safe_log_filter_removes_secrets_from_nested_exception_group() -> None:
    failure = ExceptionGroup(
        "provider batch failed",
        [
            RuntimeError("ordinary provider failure"),
            RuntimeError("Cookie: session=GROUP-COOKIE-SECRET"),
        ],
    )

    rendered = _formatted_log(
        "app.services.notifier",
        "Telegram notification failed",
        exc_info=failure,
    )

    assert "GROUP-COOKIE-SECRET" not in rendered
    assert "Telegram notification failed" in rendered
    assert "exception_type=ExceptionGroup" in rendered


def test_uvicorn_access_filter_handles_real_five_argument_records() -> None:
    callback = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        "/api/vpn-portal/auth/telegram/callback?code=CODE&state=STATE",
        "1.1",
        302,
    )
    webhook = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "POST",
        "/api/vpn-telegram/webhook/WEBHOOK-SECRET?delivery=INTERNAL",
        "1.1",
        200,
    )
    ordinary = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        "/api/domains?status=active",
        "1.1",
        200,
    )
    portal_api = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        "/api/vpn-portal/me?access_token=PORTAL-TOKEN&code=PORTAL-CODE",
        "1.1",
        200,
    )
    cabinet = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        "/cabinet/?tgWebAppData=CABINET-INIT-DATA",
        "1.1",
        200,
    )

    assert "/api/vpn-portal/auth/telegram/callback" in callback
    assert "CODE" not in callback
    assert "STATE" not in callback
    assert "/api/vpn-telegram/webhook/redacted" in webhook
    assert "WEBHOOK-SECRET" not in webhook
    assert "INTERNAL" not in webhook
    assert "/api/domains?status=active" in ordinary
    assert "/api/vpn-portal/me" in portal_api
    assert "PORTAL-TOKEN" not in portal_api
    assert "PORTAL-CODE" not in portal_api
    assert "/cabinet/" in cabinet
    assert "CABINET-INIT-DATA" not in cabinet


def test_uvicorn_access_filter_preserves_access_formatter_contract() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    handler.addFilter(SafeLogFilter())
    logger = logging.getLogger("uvicorn.access")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers.append(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:54321",
            "GET",
            "/api/vpn-portal/auth/telegram/callback?code=FORMATTER-CODE",
            "1.1",
            302,
        )
    finally:
        logger.handlers.remove(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    rendered = stream.getvalue()
    assert '127.0.0.1:54321 - "GET /api/vpn-portal/auth/telegram/callback HTTP/1.1" 302' in rendered
    assert "FORMATTER-CODE" not in rendered


def test_safe_log_filter_fails_closed_for_malformed_records() -> None:
    access_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "http://[/?code=RAW-CODE", "1.1", 400),
        None,
    )

    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("raw logging failure")

    malformed_record = logging.LogRecord(
        "httpx",
        logging.ERROR,
        __file__,
        1,
        Unprintable(),
        (),
        None,
    )

    assert SafeLogFilter().filter(access_record) is True
    assert access_record.args[2] == "/invalid-request-target"
    assert SafeLogFilter().filter(malformed_record) is True
    assert malformed_record.getMessage() == "unformattable log record [REDACTED]"


def test_logging_installation_is_idempotent_and_suppresses_http_client_chatter() -> None:
    tracked = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    previous_levels = [logger.level for logger in tracked]
    try:
        install_safe_logging()
        install_safe_logging()

        for logger in tracked:
            assert logger.getEffectiveLevel() >= logging.WARNING
            assert sum(isinstance(item, SafeLogFilter) for item in logger.filters) == 1
        access_logger = logging.getLogger("uvicorn.access")
        assert sum(
            isinstance(item, SafeLogFilter) for item in access_logger.filters
        ) == 1
    finally:
        for logger, level in zip(tracked, previous_levels, strict=True):
            logger.setLevel(level)


def test_logging_installation_suppresses_explicit_child_http_debug_levels() -> None:
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    child = logging.getLogger("httpcore.connection")
    previous_level = child.level
    previous_propagate = child.propagate
    child.setLevel(logging.DEBUG)
    child.propagate = True
    try:
        install_safe_logging()
        child.info("connect_tcp.started host=BOT-URL-SECRET")
        child.error(
            "connect failed https://api.telegram.org/botBOT-TOKEN/getUpdates"
        )
    finally:
        root.removeHandler(handler)
        child.setLevel(previous_level)
        child.propagate = previous_propagate

    rendered = stream.getvalue()
    assert "connect_tcp.started" not in rendered
    assert "BOT-URL-SECRET" not in rendered
    assert "ERROR httpcore.connection connect failed" in rendered
    assert "BOT-TOKEN" not in rendered


def test_production_uvicorn_entrypoint_installs_safe_logging() -> None:
    from app import main as main_module

    service_file = Path(__file__).resolve().parents[2] / "deploy" / "domain-drop-control.service"
    service = service_file.read_text()

    assert main_module.app is not None
    assert "uvicorn app.main:app" in service
    assert any(
        isinstance(item, SafeLogFilter)
        for item in logging.getLogger("uvicorn.access").filters
    )


def _portal_scope(path: str = "/api/vpn-portal/me") -> dict[str, object]:
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(settings=Settings())),
    }


async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.asyncio
async def test_portal_failures_before_headers_are_static_and_unlinked(caplog) -> None:
    async def failing(scope, receive, send) -> None:
        del scope, receive, send
        raise RuntimeError("cookie=EARLY-COOKIE code=EARLY-CODE")

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(failing, portal_prefix=PORTAL_PREFIX)
    with caplog.at_level("ERROR", logger="app.services.vpn_portal_http"):
        await middleware(_portal_scope(), _receive, send)

    starts = [item for item in sent if item["type"] == "http.response.start"]
    bodies = [item for item in sent if item["type"] == "http.response.body"]
    assert len(starts) == 1
    assert starts[0]["status"] == 500
    assert (b"cache-control", b"no-store") in starts[0]["headers"]
    assert bodies[-1].get("more_body", False) is False
    assert bodies[-1]["body"] == b'{"detail":"customer_service_unavailable"}'
    assert caplog.messages == ["Unhandled VPN portal request failure"]
    assert all(record.exc_info is None for record in caplog.records)
    assert "EARLY-COOKIE" not in caplog.text
    assert "EARLY-CODE" not in caplog.text


@pytest.mark.asyncio
async def test_portal_callback_boundary_survives_cookie_cleanup_failure(
    caplog, monkeypatch
) -> None:
    async def failing(scope, receive, send) -> None:
        del scope, receive, send
        raise RuntimeError("database failure DB-SECRET")

    def failing_cookie_cleanup(response, settings) -> None:
        del response, settings
        raise RuntimeError("cookie cleanup COOKIE-SECRET")

    monkeypatch.setattr(
        portal_http_module, "delete_binding_cookie", failing_cookie_cleanup
    )
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(failing, portal_prefix=PORTAL_PREFIX)
    with caplog.at_level("ERROR", logger="app.services.vpn_portal_http"):
        await middleware(
            _portal_scope("/api/vpn-portal/auth/telegram/callback"),
            _receive,
            send,
        )

    starts = [item for item in sent if item["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 500
    assert b"veltrix_login_binding=" in dict(starts[0]["headers"])[b"set-cookie"]
    assert "DB-SECRET" not in caplog.text
    assert "COOKIE-SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_portal_failures_after_headers_finish_once_without_rethrow(caplog) -> None:
    async def started_then_failed(scope, receive, send) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"partial":', "more_body": True})
        raise RuntimeError("Authorization: Bearer LATE-TOKEN")

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(started_then_failed, portal_prefix=PORTAL_PREFIX)
    with caplog.at_level("ERROR", logger="app.services.vpn_portal_http"):
        await middleware(_portal_scope(), _receive, send)

    assert sum(item["type"] == "http.response.start" for item in sent) == 1
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert caplog.messages == ["VPN portal response terminated after an internal failure"]
    assert all(record.exc_info is None for record in caplog.records)
    assert "LATE-TOKEN" not in caplog.text


@pytest.mark.asyncio
async def test_portal_send_failure_cannot_trigger_a_second_response_start(caplog) -> None:
    async def response_app(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.start":
            raise RuntimeError("transport Authorization: Bearer SEND-TOKEN")

    middleware = PortalHttpMiddleware(response_app, portal_prefix=PORTAL_PREFIX)
    with caplog.at_level("ERROR", logger="app.services.vpn_portal_http"):
        await middleware(_portal_scope(), _receive, send)

    assert sum(item["type"] == "http.response.start" for item in sent) == 1
    assert "SEND-TOKEN" not in caplog.text


@pytest.mark.asyncio
async def test_nonportal_failures_keep_normal_exception_behavior() -> None:
    failure = RuntimeError("ordinary diagnostic")

    async def failing(scope, receive, send) -> None:
        del scope, receive, send
        raise failure

    middleware = PortalHttpMiddleware(failing, portal_prefix=PORTAL_PREFIX)

    with pytest.raises(RuntimeError) as caught:
        await middleware(_portal_scope("/api/domains"), _receive, lambda message: None)

    assert caught.value is failure
    assert str(caught.value) == "ordinary diagnostic"


def test_nginx_snippets_separate_http_and_server_context_safety() -> None:
    deploy_dir = Path(__file__).resolve().parents[2] / "deploy"
    http_config = (deploy_dir / "nginx-vpn-portal-http.conf").read_text()
    server_config = (deploy_dir / "nginx-vpn-portal-locations.conf").read_text()

    assert "limit_req_zone $binary_remote_addr zone=veltrix_auth:10m rate=10r/m;" in http_config
    assert "map $uri $veltrix_safe_uri" in http_config
    assert "map $uri $veltrix_portal_cache_control" in http_config
    assert '~^/api/vpn-portal(?:/|$)  "no-store";' in http_config
    assert "/api/vpn-telegram/webhook/redacted" in http_config
    assert "$request " not in http_config
    assert "$request_uri" not in http_config
    assert "$http_" not in http_config

    assert "error_log /dev/null;" in server_config
    assert "access_log /var/log/nginx/veltrix-access.log veltrix_safe;" in server_config
    assert (
        "add_header Cache-Control $veltrix_portal_cache_control always;"
        in server_config
    )
    assert "limit_req zone=veltrix_auth burst=10 nodelay;" in server_config
    assert "limit_req_status 429;" in server_config
    assert "client_max_body_size 16k;" in server_config
    assert server_config.count('add_header Cache-Control "no-store" always;') >= 2
    assert "proxy_hide_header Cache-Control;" in server_config
    assert 'add_header Referrer-Policy "no-referrer" always;' in server_config
    assert "X-Frame-Options" not in server_config
    assert "proxy_pass http://127.0.0.1:8000;" in server_config
    assert "worker-direct-8080" in server_config
    assert "port-80" in server_config
    assert "copy the next three directives" in server_config
