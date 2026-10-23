# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Upstream HTTP client: pooled httpx.AsyncClients, one per route (TLS verification, plus the outbound proxy if any).

GETs are retried on connect failures and 502/503/504 — twice, with exponential
backoff and jitter. Nothing else is idempotent here, so nothing else retries.

Every client is built with trust_env=False: HTTP(S)_PROXY, ALL_PROXY, NO_PROXY, SSL_CERT_FILE
and SSL_CERT_DIR from the shell that launched the server are ignored, so Direct means direct.
After an outbound proxy changes, clients built for its old settings retire: they take no new
requests and are closed once their in-flight requests finish.
"""

import asyncio
import random
import ssl
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from proxy.db import Upstream
from proxy.log import get_logger
from proxy.outbound import OutboundProxyFailure, Route, tls_context

log = get_logger(__name__, "UPSTRM")

RETRIES = 2                                            # extra tries after the first, GET only
RETRYABLE_STATUSES = (502, 503, 504)
RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)   # the request never reached the origin
_BACKOFF_BASE_S = 0.25
_JITTER_S = 0.1


def _backoff_s(retry: int, rng: random.Random) -> float:
	"""0.25s, 0.5s, ... plus up to 100ms of jitter."""
	return _BACKOFF_BASE_S * (2 ** retry) + rng.uniform(0.0, _JITTER_S)


def _refusing_jar() -> CookieJar:
	"""A cookie jar that stores nothing. httpx's default jar would keep an upstream's
	Set-Cookie and replay it on later requests made for *other* identities."""
	return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def make_transport(verify_tls: bool, route: Route | None) -> httpx.AsyncBaseTransport:
	"""The real network, direct or through route. The upstream's certificate is checked against
	httpx's default trust plus the route's CA bundle."""
	return httpx.AsyncHTTPTransport(
		verify=tls_context(verify_tls, route.ca_bundle if route else None),
		trust_env=False,
		proxy=route.httpx_proxy() if route else None,
	)


def upstream_url(upstream: Upstream, path: str, raw_query: str) -> str:
	"""Append the client's path to base_url untouched, keeping the raw (percent-encoded) path and query.
	The path must start with "/": anything else ("@evil.test/x") could change the host the token goes to."""
	if not path.startswith("/"):
		raise ValueError(f"upstream path must start with '/': {path[:64]!r}")
	url = upstream.base_url + path
	return f"{url}?{raw_query}" if raw_query else url


class UpstreamClient:
	def __init__(
		self,
		transport: httpx.AsyncBaseTransport | None = None,
		sleep=asyncio.sleep,
		rng: random.Random | None = None,
	):
		self._transport = transport   # tests inject httpx.MockTransport here; it then serves every route
		self._sleep = sleep           # tests inject a recorder here
		self._rng = rng or random.Random()
		self._clients: dict[tuple, httpx.AsyncClient] = {}     # (verify_tls, proxy name, generation)
		self._in_flight: dict[httpx.AsyncClient, int] = {}
		self._retired: set[httpx.AsyncClient] = set()
		self._generation = -1                                  # proxies_version of the newest load

	def _client(self, verify_tls: bool, route: Route | None) -> httpx.AsyncClient:
		key = (verify_tls, None, None) if route is None else (verify_tls, route.name, route.generation)
		client = self._clients.get(key)
		if client is not None:
			return client
		try:
			transport = self._transport or make_transport(verify_tls, route)
		except (OSError, ssl.SSLError, ImportError) as exc:
			if route is None:
				raise
			raise OutboundProxyFailure(f"{route.label} could not be set up: {type(exc).__name__}", 503) from exc
		client = httpx.AsyncClient(
			transport=transport, trust_env=False, follow_redirects=False, cookies=_refusing_jar(),
		)
		if route is not None and route.generation < self._generation:
			self._retired.add(client)   # resolved just before a reload: serve this request, then close
		else:
			self._clients[key] = client
		return client

	async def retire_proxied(self, generation: int) -> None:
		"""OutboundProxies calls this after each load. Clients built for an older generation take no
		new requests; idle ones close now, busy ones when their last request finishes."""
		self._generation = generation
		for key in [k for k in self._clients if k[1] is not None and k[2] < generation]:
			self._retired.add(self._clients.pop(key))
		for client in [c for c in self._retired if not self._in_flight.get(c)]:
			await self._close_retired(client)

	async def _close_retired(self, client: httpx.AsyncClient) -> None:
		self._retired.discard(client)
		await client.aclose()
		log.debug("retired client closed; open=%d retired=%d", len(self._clients), len(self._retired))

	async def send(
		self,
		upstream: Upstream,
		method: str,
		path: str,
		raw_query: str,
		headers: list[tuple[str, str]],
		body: bytes,
		route: Route | None = None,
	) -> httpx.Response:
		"""Forward one request, directly or through route, retrying GETs. Raises httpx.HTTPError on
		final transport failure; one caused by the outbound proxy is an OutboundProxyFailure."""
		url = upstream_url(upstream, path, raw_query)
		encoded = [(name.encode("latin-1"), value.encode("latin-1")) for name, value in headers]
		client = self._client(upstream.verify_tls, route)
		self._in_flight[client] = self._in_flight.get(client, 0) + 1
		try:
			return await self._attempts(client, upstream, method, url, encoded, body, route)
		finally:
			self._in_flight[client] -= 1
			if not self._in_flight[client]:
				del self._in_flight[client]
				if client in self._retired:
					await self._close_retired(client)

	async def _attempts(self, client, upstream, method, url, encoded, body, route) -> httpx.Response:
		via = route.name if route else "direct"
		tries = 1 + (RETRIES if method == "GET" else 0)
		log.debug("forwarding; method=%s url=%s proxy=%s", method, url, via)
		for attempt in range(tries):
			if attempt:
				await self._sleep(_backoff_s(attempt - 1, self._rng))
				log.debug("retrying; attempt=%d url=%s proxy=%s", attempt, url, via)
			try:
				response = await client.request(
					method, url, headers=encoded, content=body or None, timeout=upstream.timeout_s,
				)
			except RETRYABLE_ERRORS as exc:
				log.debug("connect failed; attempt=%d url=%s proxy=%s error=%s", attempt, url, via, type(exc).__name__)
				if attempt < tries - 1:
					continue
				if route is not None:
					raise OutboundProxyFailure(
						f"connecting via {route.label} failed: {type(exc).__name__}: {str(exc)[:200]}"
					) from exc
				raise
			except httpx.ProxyError as exc:
				if route is None:
					raise
				raise OutboundProxyFailure(
					f"{route.label} refused the tunnel to upstream '{upstream.upstream_id}' ({str(exc) or type(exc).__name__})"
				) from exc
			if (
				route is not None and response.status_code == 407 and url.startswith("http://")
				and route.url.startswith(("http://", "https://"))
			):
				# A forwarded request through an HTTP(S) proxy: this 407 is the proxy's, and the client has
				# no relationship with it. Through SOCKS a 407 can only be the origin's.
				problem = "rejected credentials" if route.username else "requires credentials; none are configured"
				raise OutboundProxyFailure(f"{route.label} {problem} (407) for upstream '{upstream.upstream_id}'")
			if response.status_code in RETRYABLE_STATUSES and attempt < tries - 1:
				log.debug("retryable status; attempt=%d status=%d url=%s proxy=%s", attempt, response.status_code, url, via)
				continue
			log.debug("upstream replied; status=%d url=%s proxy=%s", response.status_code, url, via)
			return response

	async def aclose(self) -> None:
		for client in [*self._clients.values(), *self._retired]:
			await client.aclose()
		self._clients.clear()
		self._retired.clear()
