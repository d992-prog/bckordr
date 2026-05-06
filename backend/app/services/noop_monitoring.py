from __future__ import annotations


class NoopMonitoringOrchestrator:
    async def bootstrap(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def ensure_domain(self, domain_id: int) -> None:
        del domain_id
        return None

    async def stop_domain(self, domain_id: int) -> bool:
        del domain_id
        return False

    def worker_count(self) -> int:
        return 0
