# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbound proxies against real sockets: a tiny in-process server plays the origin or the forward
proxy, so nothing mocks httpx internals. It answers every request itself and never dials onward."""

import asyncio
import base64

import pytest

from proxy.db import Upstream
from proxy.outbound import OutboundProxyFailure, Route
from proxy.upstream import UpstreamClient


class TinyServer:
	def __init__(self, status: int = 200, body: bytes = b"ok"):
		self.status, self.body = status, body
		self.seen: list[tuple[str, dict[str, str]]] = []   # (request line, lower-cased headers)
		self.port = 0
		self._server = None

	async def start(self) -> "TinyServer":
		self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
		self.port = self._server.sockets[0].getsockname()[1]
		return self

	async def _handle(self, reader, writer):
		head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
		line, *rest = head.split("\r\n")
		headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in rest if h)}
		self.seen.append((line, headers))
		writer.write(
			b"HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
			% (self.status, len(self.body), self.body)
		)
		await writer.drain()
		writer.close()

	async def stop(self):
		if self._server.is_serving():
			self._server.close()
			await self._server.wait_closed()


@pytest.fixture
async def serve():
	started: list[TinyServer] = []

	async def _serve(**kwargs) -> TinyServer:
		server = await TinyServer(**kwargs).start()
		started.append(server)
		return server

	yield _serve
	for server in started:
		await server.stop()


async def no_sleep(seconds):
	pass


def corp(port: int, **overrides) -> Route:
	fields = dict(name="corp", url=f"http://127.0.0.1:{port}", username="egress", verify_tls=True,
		ca_bundle=None, generation=1, password="pw-secret-1")
	fields.update(overrides)
	return Route(**fields)


async def test_direct_ignores_proxy_environment(serve, monkeypatch):
	origin, trap = await serve(body=b"direct"), await serve(body=b"trapped")
	for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
		monkeypatch.setenv(var, f"http://127.0.0.1:{trap.port}")
	monkeypatch.delenv("NO_PROXY", raising=False)
	monkeypatch.delenv("no_proxy", raising=False)
	client = UpstreamClient()
	try:
		r = await client.send(Upstream("o", f"http://127.0.0.1:{origin.port}"), "GET", "/x", "", [], b"")
	finally:
		await client.aclose()
	assert r.content == b"direct"
	assert trap.seen == []
	assert origin.seen[0][0] == "GET /x HTTP/1.1"


async def test_netrc_adds_no_credentials(serve, monkeypatch, tmp_path):
	origin = await serve()
	netrc = tmp_path / "netrc"
	netrc.write_text("machine 127.0.0.1 login someone password from-netrc\n")
	monkeypatch.setenv("NETRC", str(netrc))
	client = UpstreamClient()
	try:
		await client.send(Upstream("o", f"http://127.0.0.1:{origin.port}"), "GET", "/x", "", [], b"")
	finally:
		await client.aclose()
	assert "authorization" not in origin.seen[0][1]


async def test_forwarded_request_carries_proxy_authorization(serve):
	proxy = await serve(body=b"via-proxy")
	client = UpstreamClient()
	try:
		r = await client.send(
			Upstream("jira", "http://upstream.invalid"), "GET", "/x", "", [], b"", route=corp(proxy.port),
		)
	finally:
		await client.aclose()
	assert r.content == b"via-proxy"
	line, headers = proxy.seen[0]
	assert line == "GET http://upstream.invalid/x HTTP/1.1"
	assert headers["proxy-authorization"] == "Basic " + base64.b64encode(b"egress:pw-secret-1").decode()


async def test_connect_refused_by_the_proxy(serve):
	proxy = await serve(status=407)
	client = UpstreamClient()
	try:
		with pytest.raises(OutboundProxyFailure) as caught:
			await client.send(Upstream("jira", "https://upstream.invalid"), "GET", "/x", "", [], b"", route=corp(proxy.port))
	finally:
		await client.aclose()
	assert caught.value.status == 502
	assert "outbound proxy 'corp'" in str(caught.value) and "407" in str(caught.value)
	line, headers = proxy.seen[0]
	assert line.startswith("CONNECT upstream.invalid:443 ")
	assert "proxy-authorization" in headers
	assert "pw-secret-1" not in str(caught.value)


async def test_forwarded_407_is_rejected_credentials(serve):
	proxy = await serve(status=407)
	client = UpstreamClient()
	try:
		with pytest.raises(OutboundProxyFailure, match="rejected credentials") as caught:
			await client.send(Upstream("jira", "http://upstream.invalid"), "GET", "/x", "", [], b"", route=corp(proxy.port))
	finally:
		await client.aclose()
	assert caught.value.status == 502


async def test_unreachable_proxy(serve):
	gone = await serve()
	port = gone.port
	await gone.stop()
	client = UpstreamClient(sleep=no_sleep)
	try:
		with pytest.raises(OutboundProxyFailure, match="connecting via outbound proxy 'corp'") as caught:
			await client.send(Upstream("jira", "http://upstream.invalid"), "GET", "/x", "", [], b"", route=corp(port))
	finally:
		await client.aclose()
	assert caught.value.status == 502
