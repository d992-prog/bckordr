from pathlib import Path


DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"
RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "vpn-customer-portal-runbook.md"
CLOUDFLARE_RANGES = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def _location(config: str, declaration: str) -> str:
    marker = f"location {declaration} {{"
    _, found, remainder = config.partition(marker)
    assert found, f"missing nginx location: {declaration}"
    body, found, _ = remainder.partition("\n}")
    assert found, f"unterminated nginx location: {declaration}"
    return body


def _map(config: str, variable: str) -> str:
    marker = f"map $uri ${variable} {{"
    _, found, remainder = config.partition(marker)
    assert found, f"missing nginx map: {variable}"
    body, found, _ = remainder.partition("\n}")
    assert found, f"unterminated nginx map: {variable}"
    return body


def _directives(block: str) -> set[str]:
    return {" ".join(line.split()) for line in block.splitlines() if line.strip()}


def test_real_ip_trusts_only_current_cloudflare_ranges_before_limit_zones() -> None:
    http_config = (DEPLOY_DIR / "nginx-vpn-portal-http.conf").read_text()
    trusted_ranges = tuple(
        line.strip().split()[1].removesuffix(";")
        for line in http_config.splitlines()
        if line.strip().startswith("set_real_ip_from ")
    )

    assert trusted_ranges == CLOUDFLARE_RANGES
    assert "0.0.0.0/0" not in trusted_ranges
    assert "::/0" not in trusted_ranges
    header_index = http_config.index("real_ip_header CF-Connecting-IP;")
    recursive_index = http_config.index("real_ip_recursive on;")
    first_zone_index = http_config.index("limit_req_zone ")
    assert max(http_config.index(f"set_real_ip_from {cidr};") for cidr in trusted_ranges) < (
        header_index
    )
    assert header_index < recursive_index < first_zone_index
    assert (
        "log_format veltrix_safe '$remote_addr $request_method $veltrix_safe_uri "
        "$status $body_bytes_sent';" in http_config
    )
    assert "$http_cf_connecting_ip" not in http_config.lower()


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
    http_config = (DEPLOY_DIR / "nginx-vpn-portal-http.conf").read_text()
    locations = (DEPLOY_DIR / "nginx-vpn-portal-locations.conf").read_text()
    redirect = _location(locations, "= /vpn")
    public_page = _location(locations, "^~ /vpn/")
    server_scope = locations.partition("location ")[0]

    assert "return 308 /vpn/;" in redirect
    assert "proxy_pass http://127.0.0.1:8000;" in public_page
    assert "proxy_hide_header Cache-Control;" in public_page
    assert '~^/vpn(?:/|$) "no-cache, max-age=0, must-revalidate";' in _directives(
        _map(http_config, "veltrix_portal_cache_control")
    )
    assert '~^/vpn(?:/|$) "no-referrer";' in _directives(
        _map(http_config, "veltrix_portal_referrer_policy")
    )
    assert '~^/vpn(?:/|$) "nosniff";' in _directives(
        _map(http_config, "veltrix_portal_content_type_options")
    )
    assert '~^/vpn(?:/|$) "DENY";' in _directives(
        _map(http_config, "veltrix_portal_frame_options")
    )
    assert '~^/vpn(?:/|$) "frame-ancestors \'none\'";' in _directives(
        _map(http_config, "veltrix_portal_content_security_policy")
    )
    assert "add_header Cache-Control $veltrix_portal_cache_control always;" in server_scope
    assert "add_header Referrer-Policy $veltrix_portal_referrer_policy always;" in server_scope
    assert (
        "add_header X-Content-Type-Options $veltrix_portal_content_type_options always;"
        in server_scope
    )
    assert "add_header X-Frame-Options $veltrix_portal_frame_options always;" in server_scope
    assert (
        "add_header Content-Security-Policy "
        "$veltrix_portal_content_security_policy always;" in server_scope
    )


def test_portal_api_and_cabinet_remain_no_store_without_blocking_mini_app() -> None:
    http_config = (DEPLOY_DIR / "nginx-vpn-portal-http.conf").read_text()
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

    public_page = _location(locations, "^~ /vpn/")
    for block in (*api_blocks, *cabinet_blocks, public_page):
        assert "proxy_hide_header Cache-Control;" in block
    for block in (
        *api_blocks,
        *cabinet_blocks,
        public_page,
        _location(locations, "= /vpn"),
        _location(locations, "^~ /api/vpn-telegram/webhook/"),
    ):
        assert "add_header " not in block

    cache_map = _map(http_config, "veltrix_portal_cache_control")
    referrer_map = _map(http_config, "veltrix_portal_referrer_policy")
    nosniff_map = _map(http_config, "veltrix_portal_content_type_options")
    frame_map = _map(http_config, "veltrix_portal_frame_options")
    csp_map = _map(http_config, "veltrix_portal_content_security_policy")
    assert '~^/api/vpn-portal(?:/|$) "no-store";' in _directives(cache_map)
    assert '~^/cabinet(?:/|$) "no-store";' in _directives(cache_map)
    for policy_map, value in ((referrer_map, "no-referrer"), (nosniff_map, "nosniff")):
        assert f'~^/api/vpn-portal(?:/|$) "{value}";' in _directives(policy_map)
        assert f'~^/cabinet(?:/|$) "{value}";' in _directives(policy_map)
    assert _directives(frame_map) == {'~^/vpn(?:/|$) "DENY";', 'default "";'}
    assert _directives(csp_map) == {
        '~^/vpn(?:/|$) "frame-ancestors \'none\'";',
        'default "";',
    }


def test_runbook_uses_validated_backup_and_atomic_nginx_file_replacement() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "docs/veltrix-release-operations.md",
        "sudo nginx -T",
        "sudo systemctl start veltrix-backup.service",
        "latest-success.json",
        "backup.json",
        "manifest.json",
        "pg_restore --list",
        "/usr/bin/python3",
        "sha256sum --check --strict",
        "/etc/nginx/conf.d/.veltrix-portal.conf.new",
        "/etc/nginx/snippets/.veltrix-vpn-portal-locations.conf.new",
        "/etc/nginx/sites-available/.domain-drop-control.rollback",
        "/etc/nginx/sites-available/.domain-drop-worker-runtime-direct.rollback",
        "/etc/nginx/conf.d/.veltrix-portal.conf.rollback",
        "/etc/nginx/snippets/.veltrix-vpn-portal-locations.conf.rollback",
        "sudo mv -fT",
    ):
        assert required in runbook
    assert runbook.index(
        "sudo mv -fT /etc/nginx/conf.d/.veltrix-portal.conf.new"
    ) < runbook.index(
        "sudo mv -fT /etc/nginx/snippets/.veltrix-vpn-portal-locations.conf.new"
    )
    assert runbook.index(
        "sudo mv -fT /etc/nginx/snippets/.veltrix-vpn-portal-locations.conf.rollback"
    ) < runbook.index(
        "sudo mv -fT /etc/nginx/conf.d/.veltrix-portal.conf.rollback"
    )
    assert "Создать новый закрытый каталог резервной копии" not in runbook
    assert "Создать новую серверную копию полной БД" not in runbook


def test_runbook_verifies_cloudflare_real_ip_without_logging_headers() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "https://www.cloudflare.com/ips-v4",
        "https://www.cloudflare.com/ips-v6",
        "nginx -V",
        "http_realip_module",
        "set_real_ip_from",
        "real_ip_header CF-Connecting-IP",
        "real_ip_recursive on",
        "двух реальных клиентских сетей",
        "прямой запрос к origin",
        "Не журналировать CF-Connecting-IP",
    ):
        assert required in runbook
