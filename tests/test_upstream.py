# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import gzip

import httpx
import pytest

from proxy.db import Upstream
from proxy.upstream import UpstreamClient, upstream_url

JIRA = Upstream("jira", "https://jira.test", timeout_s=5.0)


def recording_client(responder):
	seen = []

	def handler(request):
		seen.append(request)
		return responder(request)

	return UpstreamClient(transport=httpx.MockTransport(handler)), seen


def test_upstream_url_appends_the_path_untouched_to_base_url():
	assert upstream_url(JIRA, "/rest/api/2/issue/ABC-1", "") == "https://jira.test/rest/api/2/issue/ABC-1"
	assert upstream_url(JIRA, "/", "") == "https://jira.test/"
	assert upstream_url(JIRA, "/jira/x", "") == "https://jira.test/jira/x"   # no segment is ever taken as a prefix
	assert upstream_url(Upstream("j", "https://h.test/jira"), "/rest/x", "") == "https://h.test/jira/rest/x"
	assert upstream_url(JIRA, "/search", "jql=a%3Db&x=1") == "https://jira.test/search?jql=a%3Db&x=1"


async def test_send_forwards_method_url_headers_body_and_timeout():
	client, seen = recording_client(lambda req: httpx.Response(201, json={"id": 1}))
	resp = await client.send(
		JIRA, "POST", "/rest/issue", "",
		[("Authorization", "Basic abc"), ("Content-Type", "application/json")], b'{"a":1}',
	)
	assert resp.status_code == 201
	req = seen[0]
	assert req.method == "POST"
	assert str(req.url) == "https://jira.test/rest/issue"
	assert req.headers["authorization"] == "Basic abc"
	assert req.content == b'{"a":1}'
	assert req.extensions["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}
	await client.aclose()


async def test_percent_encoding_reaches_upstream_byte_for_byte():
	client, seen = recording_client(lambda req: httpx.Response(200))
	await client.send(JIRA, "GET", "/rest/a%2Fb%20c", "q=a%26b", [], b"")
	assert seen[0].url.raw_path == b"/rest/a%2Fb%20c?q=a%26b"
	await client.aclose()


async def test_gzip_body_is_decoded():
	payload = b'{"ok":true}'
	client, _ = recording_client(
		lambda req: httpx.Response(200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip"}),
	)
	resp = await client.send(JIRA, "GET", "/x", "", [], b"")
	assert resp.content == payload
	await client.aclose()


async def test_latin1_header_values_are_forwarded():
	client, seen = recording_client(lambda req: httpx.Response(200))
	await client.send(JIRA, "GET", "/x", "", [("X-Name", "café")], b"")
	assert seen[0].headers.raw[-1] == (b"X-Name", "café".encode("latin-1"))
	await client.aclose()


async def test_upstream_cookies_are_never_replayed():
	seen = []

	def handler(request: httpx.Request) -> httpx.Response:
		seen.append(request.headers.get("cookie"))
		return httpx.Response(200, headers={"Set-Cookie": "JSESSIONID=abc; Path=/"})

	client = UpstreamClient(transport=httpx.MockTransport(handler))
	upstream = Upstream("jira", "https://jira.test")
	try:
		await client.send(upstream, "GET", "/a", "", [], b"")
		await client.send(upstream, "GET", "/b", "", [], b"")
	finally:
		await client.aclose()
	assert seen == [None, None]


import asyncio

from proxy.outbound import OutboundProxyFailure, Route


def route(generation=1, **overrides) -> Route:
	fields = dict(name="corp", url="http://10.0.0.5:3128", username="egress", verify_tls=True,
		ca_bundle=None, generation=generation, password="pw")
	fields.update(overrides)
	return Route(**fields)


async def test_each_route_gets_its_own_pooled_client():
	client = UpstreamClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
	direct, unverified, corp = client._client(True, None), client._client(False, None), client._client(True, route())
	assert len({id(direct), id(unverified), id(corp)}) == 3
	assert client._client(True, route()) is corp
	assert all(c.trust_env is False for c in (direct, unverified, corp))
	await client.aclose()


async def test_retire_closes_idle_proxied_clients_only():
	client = UpstreamClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
	direct, corp = client._client(True, None), client._client(True, route(1))
	await client.retire_proxied(2)
	assert corp.is_closed and not direct.is_closed
	assert client._client(True, route(2)) is not corp
	await client.aclose()


async def test_in_flight_request_finishes_on_a_retired_client_then_it_closes():
	started, release = asyncio.Event(), asyncio.Event()

	async def handler(request):
		started.set()
		await release.wait()
		return httpx.Response(200, text="old")

	client = UpstreamClient(transport=httpx.MockTransport(handler))
	old = route(1)
	pooled = client._client(JIRA.verify_tls, old)
	task = asyncio.create_task(client.send(JIRA, "GET", "/x", "", [], b"", route=old))
	await started.wait()
	await client.retire_proxied(2)
	assert not pooled.is_closed
	release.set()
	assert (await task).text == "old"
	assert pooled.is_closed
	await client.aclose()


async def test_route_resolved_before_a_reload_is_not_pooled():
	client = UpstreamClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
	await client.retire_proxied(5)
	stale = route(4)
	await client.send(JIRA, "GET", "/x", "", [], b"", route=stale)
	assert client._client(True, route(5)) is not client._client(True, stale)
	assert (True, "corp", 4) not in client._clients
	await client.aclose()


async def test_unreadable_ca_bundle_is_503_before_any_network(tmp_path):
	client = UpstreamClient()   # the real transport factory
	broken = route(ca_bundle=str(tmp_path / "gone.pem"))
	with pytest.raises(OutboundProxyFailure) as caught:
		await client.send(JIRA, "GET", "/x", "", [], b"", route=broken)
	assert caught.value.status == 503 and "outbound proxy 'corp'" in str(caught.value)
	await client.aclose()


async def test_407_through_a_socks_route_is_the_origins_and_passes_through():
	from proxy.outbound import Route
	route = Route("gw", "socks5://127.0.0.1:1080", None, True, None, 1)
	client, seen = recording_client(lambda req: httpx.Response(407, text="origin wants auth"))
	resp = await client.send(Upstream("o", "http://origin.test"), "GET", "/o/x", "", [], b"", route)
	assert resp.status_code == 407 and resp.text == "origin wants auth"


def test_upstream_url_refuses_a_path_that_could_change_the_host():
	for path in ("@evil.test/x", ".evil.test/x", ""):
		with pytest.raises(ValueError):
			upstream_url(JIRA, path, "")
