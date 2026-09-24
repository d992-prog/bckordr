from __future__ import annotations

import logging
import re
import traceback
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REDACTED = "[REDACTED]"
_SENSITIVE_EXCEPTION_LOGGERS = (
    "app.api.routes.vpn_portal",
    "app.services.vpn_portal",
    "httpcore",
    "httpx",
    "sqlalchemy",
)
_BOT_TOKEN = re.compile(r"(?i)(/bot)[^/\s?]+")
_CONNECTION_URI = re.compile(r"(?i)\b(?:vless|vmess|trojan|ss)://[^\s\"'<>]+")
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s,;\"']+")
_SENSITIVE_HEADER_KEY = re.compile(
    r"(?i)(?<![\w-])(?:b)?[\"']?"
    r"(?:authorization|proxy-authorization|cookie|set-cookie)"
    r"[\"']?(?=\s*[:,=])"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:code|state|init_data|initdata|code_verifier|client_secret|token)=)[^&\s\"']*"
)
_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:code|state|init_data|initdata|code_verifier|client_secret|authorization|cookie)"
    r"\b[\"']?\s*[:=]\s*[\"']?)[^,\s}\"']+"
)


def _safe_access_target(target: str) -> str:
    try:
        parts = urlsplit(target)
    except (TypeError, ValueError):
        return "/invalid-request-target"
    path = parts.path
    if path.startswith("/api/vpn-telegram/webhook/"):
        return urlunsplit((parts.scheme, parts.netloc, "/api/vpn-telegram/webhook/redacted", "", ""))
    if (
        path == "/api/vpn-portal"
        or path.startswith("/api/vpn-portal/")
        or path == "/cabinet"
        or path.startswith("/cabinet/")
    ):
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return target


def _sanitize_text(value: str) -> str:
    value = _BOT_TOKEN.sub(r"\1" + REDACTED, value)
    value = _CONNECTION_URI.sub("connection-uri:" + REDACTED, value)
    value = _QUERY_SECRET.sub(r"\1" + REDACTED, value)
    header = _SENSITIVE_HEADER_KEY.search(value)
    if header is not None:
        prefix = value[: header.start()].rstrip().rstrip("{[(").rstrip()
        if not prefix:
            value = "sensitive headers " + REDACTED
        else:
            separator = "" if prefix.endswith(("=", ":")) else " "
            value = prefix + separator + REDACTED
    value = _BEARER.sub(r"\1" + REDACTED, value)
    value = _NAMED_SECRET.sub(r"\1" + REDACTED, value)
    return value


class SafeLogFilter(logging.Filter):
    """Remove customer-portal credentials before a record reaches a formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_veltrix_safe_filtered", False):
            return True
        if record.levelno < logging.WARNING and record.name.startswith(
            ("httpx", "httpcore")
        ):
            return False
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            client, method, target, version, status = args
            if isinstance(target, str):
                record.args = (
                    client,
                    method,
                    _safe_access_target(target),
                    version,
                    status,
                )
            record._veltrix_safe_filtered = True
            return True

        try:
            rendered = record.getMessage()
        except Exception:
            try:
                rendered = str(record.msg)
            except Exception:
                rendered = "unformattable log record [REDACTED]"

        lowered = rendered.lower()
        if record.name.startswith("sqlalchemy.") and record.args and (
            "vpn_portal_login_attempts" in lowered
            or "vpn_portal_mini_app_exchanges" in lowered
            or "code_verifier" in lowered
        ):
            rendered = "portal authentication SQL [REDACTED]"
        else:
            rendered = _sanitize_text(rendered)

        exception_contains_secret = False
        if record.exc_info:
            try:
                exception_text = "".join(
                    traceback.format_exception(*record.exc_info)
                )
            except Exception:
                exception_contains_secret = True
            else:
                exception_contains_secret = (
                    _sanitize_text(exception_text) != exception_text
                )
        sensitive_exception = record.name.startswith(
            _SENSITIVE_EXCEPTION_LOGGERS
        ) or exception_contains_secret
        if record.exc_info and sensitive_exception:
            exception_type = record.exc_info[0].__name__
            rendered = f"{rendered} exception_type={exception_type}"
            record.exc_info = None
            record.exc_text = None
        if sensitive_exception:
            record.stack_info = None
        record.msg = rendered
        record.args = ()
        record._veltrix_safe_filtered = True
        return True


def _add_filter_once(target: Any) -> None:
    if not any(isinstance(item, SafeLogFilter) for item in target.filters):
        target.addFilter(SafeLogFilter())


def install_safe_logging() -> None:
    """Install idempotent process logging guards after Uvicorn config is loaded."""

    root = logging.getLogger()
    for handler in root.handlers:
        _add_filter_once(handler)

    for name in (
        "app.api.routes.vpn_portal",
        "app.services.vpn_portal_auth",
        "app.services.vpn_portal_http",
        "app.services.vpn_portal_telegram",
        "sqlalchemy.engine",
        "uvicorn.access",
    ):
        _add_filter_once(logging.getLogger(name))

    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)
        _add_filter_once(logger)
