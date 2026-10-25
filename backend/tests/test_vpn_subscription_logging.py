"""Subscription URLs are bearer credentials regardless of API prefix or validity."""

import logging
from pathlib import Path

import pytest
from test_vpn_portal_logging import _formatted_log

from app.services.vpn_portal_logging import SafeLogFilter, install_safe_logging


@pytest.mark.parametrize("prefix", ["/api", "/custom", ""])
@pytest.mark.parametrize("token", ["SIGNED-SECRET", "invalid%2FSECRET", "SECRET/extra"])
def test_subscription_access_target_redacts_token_and_query(prefix, token):
    rendered = _formatted_log(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:54321",
        "GET",
        f"{prefix}/vpn-portal/subscription/{token}?extra=QUERY-SECRET#FRAGMENT-SECRET",
        "1.1",
        404,
    )
    assert f"{prefix}/vpn-portal/subscription/redacted" in rendered
    assert "SECRET" not in rendered


@pytest.mark.parametrize("prefix", ["/api", "/custom", ""])
def test_full_url_in_ordinary_log_and_exception_cannot_expose_subscription(prefix):
    target = f"https://vpn.example{prefix}/vpn-portal/subscription/TOKEN-SECRET?extra=QUERY-SECRET#FRAGMENT-SECRET"
    rendered = _formatted_log(
        "ordinary.logger",
        "Failed request %s",
        target,
        exc_info=RuntimeError(target),
        stack_info=True,
    )
    assert "SECRET" not in rendered
    assert f"{prefix}/vpn-portal/subscription/redacted" in rendered
    assert "exception_type=RuntimeError" in rendered
    assert "Stack (most recent call last)" not in rendered


def test_subscription_logging_is_installed_before_service_payloads_exist():
    install_safe_logging()
    logger = logging.getLogger("app.services.vpn_device_subscriptions")
    assert any(isinstance(item, SafeLogFilter) for item in logger.filters)


def test_nginx_redacts_subscription_paths_before_default_logging():
    config = (
        Path(__file__).resolve().parents[2] / "deploy/nginx-vpn-portal-http.conf"
    ).read_text(encoding="utf-8")
    rule = "~^/api/vpn-portal/subscription/  /api/vpn-portal/subscription/redacted;"
    safe_map = config.split("map $uri $veltrix_safe_uri {", 1)[1].split("}", 1)[0]
    assert rule in safe_map
    assert safe_map.index(rule) < safe_map.index("default")
