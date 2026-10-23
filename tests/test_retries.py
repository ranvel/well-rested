# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import httpx
import pytest

from proxy.db import Upstream
from proxy.upstream import RETRIES, RETRYABLE_STATUSES, UpstreamClient

UP = Upstream(upstream_id="jira", base_url="https://jira.example.com")


class Script:
	"""An upstream that follows a script of responses / exceptions, counting calls."""

	def __init__(self, *steps):
		self.steps = list(steps)
		self.calls = 0

	def __call__(self, request: httpx.Request) -> httpx.Response:
		self.calls += 1
		step = self.steps.pop(0)
		if isinstance(step, Exception):
			raise step
		return httpx.Response(step, text="ok" if step == 200 else "nope")


class Sleeps:
	def __init__(self):
		self.delays: list[float] = []

	async def __call__(self, seconds: float) -> None:
		self.delays.append(seconds)


def client(script: Script, sleeps: Sleeps) -> UpstreamClient:
	return UpstreamClient(transport=httpx.MockTransport(script), sleep=sleeps)


async def send(c: UpstreamClient, method: str = "GET") -> httpx.Response:
	return await c.send(UP, method, "/rest/x", "", [], b"")


async def test_connect_errors_then_success():
	script = Script(httpx.ConnectError("boom"), httpx.ConnectError("boom"), 200)
	sleeps = Sleeps()
	response = await send(client(script, sleeps))
	assert response.status_code == 200
	assert script.calls == 3
	assert len(sleeps.delays) == RETRIES
	assert 0.25 <= sleeps.delays[0] <= 0.35   # base + up to 100ms jitter
	assert 0.50 <= sleeps.delays[1] <= 0.60


async def test_retryable_status_then_success():
	script = Script(503, 200)
	response = await send(client(script, Sleeps()))
	assert response.status_code == 200
	assert script.calls == 2


async def test_connect_errors_exhausted_raise():
	script = Script(*(httpx.ConnectError("boom") for _ in range(1 + RETRIES)))
	with pytest.raises(httpx.ConnectError):
		await send(client(script, Sleeps()))
	assert script.calls == 1 + RETRIES


async def test_retryable_status_exhausted_is_returned():
	script = Script(*(502 for _ in range(1 + RETRIES)))
	response = await send(client(script, Sleeps()))
	assert response.status_code == 502
	assert script.calls == 1 + RETRIES


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
async def test_every_retryable_status_is_retried(status):
	script = Script(status, 200)
	assert (await send(client(script, Sleeps()))).status_code == 200
	assert script.calls == 2


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_only_get_retries(method):
	script = Script(httpx.ConnectError("boom"))
	with pytest.raises(httpx.ConnectError):
		await send(client(script, Sleeps()), method)
	assert script.calls == 1


async def test_500_is_not_retried():
	script = Script(500)
	response = await send(client(script, Sleeps()))
	assert response.status_code == 500
	assert script.calls == 1


async def test_read_timeout_is_not_retried():
	script = Script(httpx.ReadTimeout("slow"))
	with pytest.raises(httpx.ReadTimeout):
		await send(client(script, Sleeps()))
	assert script.calls == 1
