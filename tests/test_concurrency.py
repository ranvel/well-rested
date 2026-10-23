# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio

import httpx

from proxy import db
from proxy.keychain import MemorySecretStore
from proxy.main import create_app
from tests.test_caching import shift_clock


async def test_concurrent_misses_fetch_upstream_once(settings, monkeypatch):
	"""Ten requests missing the same key share one upstream fetch; a later wave fetches afresh."""
	calls = 0
	release = asyncio.Event()

	async def responder(request):
		nonlocal calls
		calls += 1
		await release.wait()
		return httpx.Response(200, json={"n": calls})

	async def wave(c, expected_x_cache):
		tasks = [asyncio.create_task(c.get("/x")) for _ in range(10)]
		await asyncio.sleep(0.05)   # every request has read the shelf and joined the flight
		release.set()
		responses = await asyncio.gather(*tasks)
		release.clear()
		assert [r.status_code for r in responses] == [200] * 10
		assert {r.headers["x-cache"] for r in responses} == {expected_x_cache}
		assert len({r.json()["n"] for r in responses}) == 1
		assert len(app.state.flights) == 0

	app = create_app(settings, transport=httpx.MockTransport(responder), secrets=MemorySecretStore(), proxy_secrets=MemorySecretStore())
	async with app.router.lifespan_context(app):
		conn = db.connect(settings.db_path)
		db.create_rule(conn, "jira", "/**", 60)
		conn.close()
		transport = httpx.ASGITransport(app=app)
		async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers={"X-WR-Upstream": "jira"}) as c:
			await wave(c, "MISS")
			assert calls == 1
			shift_clock(monkeypatch, 61)
			await wave(c, "REFRESH")
			assert calls == 2


async def test_slow_fetch_does_not_block_hits_on_the_same_prefix(settings):
	"""The per-shelf lock must be released during the upstream fetch."""
	reached = asyncio.Event()
	release = asyncio.Event()

	async def responder(request):
		if request.url.path == "/slow":
			reached.set()
			await release.wait()
		return httpx.Response(200, json={"path": request.url.path})

	app = create_app(settings, transport=httpx.MockTransport(responder), secrets=MemorySecretStore(), proxy_secrets=MemorySecretStore())
	async with app.router.lifespan_context(app):
		conn = db.connect(settings.db_path)
		db.create_rule(conn, "jira", "/**", 3600)
		conn.close()
		transport = httpx.ASGITransport(app=app)
		async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers={"X-WR-Upstream": "jira"}) as c:
			assert (await c.get("/fast")).headers["x-cache"] == "MISS"
			slow = asyncio.create_task(c.get("/slow"))
			try:
				await asyncio.wait_for(reached.wait(), timeout=2)
				hit = await asyncio.wait_for(c.get("/fast"), timeout=2)
				assert hit.headers["x-cache"] == "HIT"
				assert not slow.done()
			finally:
				release.set()
			assert (await slow).headers["x-cache"] == "MISS"
