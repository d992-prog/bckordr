from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"


def _location(config: str, declaration: str) -> str:
    marker = f"location {declaration} {{"
    _, found, remainder = config.partition(marker)
    assert found, f"missing nginx location: {declaration}"
    body, found, _ = remainder.partition("\n}")
    assert found, f"unterminated nginx location: {declaration}"
    return body


def test_portal_api_limit_is_separate_from_auth_and_webhook() -> None:
    http_config = (DEPLOY_DIR / "nginx-vpn-portal-http.conf").read_text()
    locations = (DEPLOY_DIR / "nginx-vpn-portal-locations.conf").read_text()

    assert (
        "limit_req_zone $binary_remote_addr zone=veltrix_portal_api:10m rate=5r/s;"
        in http_config
    )
    assert "limit_req_zone $binary_remote_addr zone=veltrix_auth:10m rate=10r/m;" in http_config

    auth = _location(locations, "^~ /api/vpn-portal/auth/")
    portal_root = _location(locations, "= /api/vpn-portal")
    portal_api = _location(locations, "^~ /api/vpn-portal/")
    webhook = _location(locations, "^~ /api/vpn-telegram/webhook/")

    assert locations.index("location ^~ /api/vpn-portal/auth/ {") < locations.index(
        "location ^~ /api/vpn-portal/ {"
    )
    assert "limit_req zone=veltrix_auth burst=10 nodelay;" in auth
    assert "veltrix_portal_api" not in auth
    for block in (portal_root, portal_api):
        assert "limit_req zone=veltrix_portal_api burst=10 nodelay;" in block
        assert "veltrix_auth" not in block
        assert "limit_req_status 429;" in block
    assert "limit_req_status 429;" in auth
    assert "limit_req " not in webhook


def test_public_vpn_page_routes_with_revalidation_and_security_headers() -> None:
    locations = (DEPLOY_DIR / "nginx-vpn-portal-locations.conf").read_text()
    redirect = _location(locations, "= /vpn")
    public_page = _location(locations, "^~ /vpn/")

    assert "return 308 /vpn/;" in redirect
    assert "proxy_pass http://127.0.0.1:8000;" in public_page
    assert "proxy_hide_header Cache-Control;" in public_page
    assert 'add_header Cache-Control "no-cache, max-age=0, must-revalidate" always;' in public_page
    assert 'add_header X-Content-Type-Options "nosniff" always;' in public_page
    assert 'add_header X-Frame-Options "DENY" always;' in public_page
    assert 'add_header Referrer-Policy "no-referrer" always;' in public_page
    assert "no-store" not in public_page


def test_portal_api_and_cabinet_remain_no_store_without_blocking_mini_app() -> None:
    locations = (DEPLOY_DIR / "nginx-vpn-portal-locations.conf").read_text()

    api_blocks = (
        _location(locations, "^~ /api/vpn-portal/auth/"),
        _location(locations, "= /api/vpn-portal"),
        _location(locations, "^~ /api/vpn-portal/"),
    )
    cabinet_blocks = (
        _location(locations, "= /cabinet"),
        _location(locations, "^~ /cabinet/"),
    )

    for block in (*api_blocks, *cabinet_blocks):
        assert "proxy_hide_header Cache-Control;" in block
        assert 'add_header Cache-Control "no-store" always;' in block
        assert 'add_header X-Content-Type-Options "nosniff" always;' in block
    for block in cabinet_blocks:
        assert "X-Frame-Options" not in block
        assert "frame-ancestors" not in block
