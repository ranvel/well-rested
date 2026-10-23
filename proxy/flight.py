# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-key single-flight: concurrent callers for one key await a single execution.

Used to stop cache stampedes: when several requests miss (or find stale) the same
cache key at once, one of them fetches upstream and the rest share the result.
"""

import asyncio
from typing import Awaitable, Callable, Hashable, TypeVar

from proxy.log import get_logger

log = get_logger(__name__, "FLGHT")

T = TypeVar("T")


class SingleFlight:
	def __init__(self):
		self._flights: dict[Hashable, asyncio.Task] = {}

	def __len__(self) -> int:
		return len(self._flights)

	async def do(self, key: Hashable, work: Callable[[], Awaitable[T]]) -> T:
		"""Run work() once per key at a time; everyone awaiting the key shares the outcome.

		The task is shielded from waiter cancellation, so a client that gives up
		doesn't abort the fetch for everyone else. The key is forgotten when the
		work finishes, so the next request starts a fresh flight.
		"""
		task = self._flights.get(key)
		if task is None:
			task = self._flights[key] = asyncio.create_task(self._run(key, work))
		else:
			log.debug("joined flight; key=%s", key)
		return await asyncio.shield(task)

	async def _run(self, key: Hashable, work: Callable[[], Awaitable[T]]) -> T:
		try:
			return await work()
		finally:
			self._flights.pop(key, None)
