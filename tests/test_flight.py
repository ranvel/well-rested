# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio

from proxy.flight import SingleFlight


async def test_concurrent_callers_share_one_execution():
	flights = SingleFlight()
	calls = 0
	release = asyncio.Event()

	async def work():
		nonlocal calls
		calls += 1
		await release.wait()
		return "result"

	tasks = [asyncio.create_task(flights.do("k", work)) for _ in range(5)]
	await asyncio.sleep(0)   # let every caller join the flight
	release.set()
	assert await asyncio.gather(*tasks) == ["result"] * 5
	assert calls == 1
	assert len(flights) == 0


async def test_distinct_keys_do_not_share():
	flights = SingleFlight()
	calls = []

	async def work(key):
		calls.append(key)
		return key

	results = await asyncio.gather(flights.do("a", lambda: work("a")), flights.do("b", lambda: work("b")))
	assert sorted(results) == ["a", "b"]
	assert sorted(calls) == ["a", "b"]


async def test_leader_failure_reaches_every_waiter():
	flights = SingleFlight()
	release = asyncio.Event()

	async def work():
		await release.wait()
		raise RuntimeError("upstream down")

	tasks = [asyncio.create_task(flights.do("k", work)) for _ in range(3)]
	await asyncio.sleep(0)
	release.set()
	results = await asyncio.gather(*tasks, return_exceptions=True)
	assert all(isinstance(r, RuntimeError) for r in results)
	assert len(flights) == 0


async def test_cancelled_waiter_does_not_kill_the_flight():
	flights = SingleFlight()
	release = asyncio.Event()

	async def work():
		await release.wait()
		return "survived"

	leader_wait = asyncio.create_task(flights.do("k", work))
	follower = asyncio.create_task(flights.do("k", work))
	await asyncio.sleep(0)
	leader_wait.cancel()                     # the first *waiter* goes away, not the work
	await asyncio.sleep(0)
	release.set()
	assert await follower == "survived"


async def test_next_request_after_completion_starts_fresh():
	flights = SingleFlight()
	calls = 0

	async def work():
		nonlocal calls
		calls += 1
		return calls

	assert await flights.do("k", work) == 1
	assert await flights.do("k", work) == 2
