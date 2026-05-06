from __future__ import annotations

from urllib.parse import urlparse

from app.db.models import Proxy


def parse_proxy_url(proxy_url: str) -> dict[str, str | int | None]:
    parsed = urlparse(proxy_url.strip())
    if parsed.scheme.lower() != "socks5":
        raise ValueError("Only socks5 proxies are supported")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Proxy must include host and port")

    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "login": parsed.username,
        "password": parsed.password,
        "type": "socks5",
    }


def build_proxy_url(proxy: Proxy) -> str:
    credentials = ""
    if proxy.login:
        credentials = proxy.login
        if proxy.password:
            credentials += f":{proxy.password}"
        credentials += "@"
    return f"{proxy.type}://{credentials}{proxy.host}:{proxy.port}"
