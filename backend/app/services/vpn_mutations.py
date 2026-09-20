"""Serialize admin changes with the single-process VPN lifecycle worker."""

import asyncio
from collections.abc import AsyncIterator
from weakref import WeakKeyDictionary


_locks: WeakKeyDictionary = WeakKeyDictionary()


def vpn_mutation_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _locks:
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def serialize_vpn_mutation() -> AsyncIterator[None]:
    async with vpn_mutation_lock():
        yield
