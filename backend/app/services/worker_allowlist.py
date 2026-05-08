from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WorkerNode


def _normalize_ip(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def render_worker_runtime_allowlist(workers) -> str:
    allowed_ips = sorted(
        {
            normalized
            for worker in workers
            if getattr(worker, "is_enabled", True)
            for normalized in [_normalize_ip(getattr(worker, "ip_address", None))]
            if normalized is not None
        }
    )
    lines = ["# Managed by Domain Drop Catcher control", "# Worker runtime allowlist"]
    for ip in allowed_ips:
        lines.append(f"allow {ip};")
    lines.append("deny all;")
    lines.append("")
    return "\n".join(lines)


async def sync_worker_runtime_allowlist(session: AsyncSession, settings) -> bool:
    allowlist_path = getattr(settings, "worker_runtime_allowlist_path", None)
    if not allowlist_path:
        return False

    workers = (
        await session.execute(select(WorkerNode).order_by(WorkerNode.id.asc()))
    ).scalars().all()
    rendered = render_worker_runtime_allowlist(workers)
    destination = Path(allowlist_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")

    reload_command = getattr(settings, "worker_runtime_allowlist_reload_command", None)
    if reload_command:
        process = await asyncio.create_subprocess_shell(
            reload_command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return (await process.wait()) == 0
    return True
