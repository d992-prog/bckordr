from __future__ import annotations

import asyncio
import logging

from app.config import WorkerSettings
from app.runner import WorkerRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def main() -> None:
    settings = WorkerSettings()
    runner = WorkerRunner(settings)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
