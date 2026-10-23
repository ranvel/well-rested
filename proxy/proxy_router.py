# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Catch-all proxy route.

The path is forwarded untouched; X-WR-Upstream picks the upstream, or, without it, the one
upstream the caller's identity is bound to. Every request is admitted: a client Authorization
is refused, X-WR-Identity is resolved to an identity bound to the upstream with a token in the
Keychain, and a non-loopback client needs an approval. Admission runs before the cache is
consulted, so a HIT is gated exactly like a MISS.

Non-GET methods always pass through. A GET passes through when no rule matches, the
rule is a bypass (ttl 0), or the rule is reject-mode and the request has a query.
Otherwise it is served from, or stored into, the upstream's shelf, in the caller's own
partition (the identity's, or the anonymous one); no entry is ever served to another caller.
A Location pointing at the upstream is pointed back through the proxy; bodies are never rewritten.
A failure caused by an outbound proxy is reported as the proxy's (502/503) and served stale like any upstream failure.
"""

import time
from dataclasses import dataclass, field
from email.utils import formatdate
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request, Response

from proxy import db
from proxy.admission import client_addr
from proxy.approvals import Verdict
from proxy.cache import ANONYMOUS, age_seconds, build_key, is_fresh, make_entry, normalize_query
from proxy.headers import (
	cacheable_response_headers,
	client_response_headers,
	decode_raw_headers,
	rewrite_location,
	upstream_request_headers,
)
from proxy.identities import HEADER
from proxy.log import get_logger
from proxy.outbound import OutboundProxyFailure
from proxy.rules import Rule
from proxy.upstream import RETRYABLE_STATUSES

log = get_logger(__name__, "PROXY")

UPSTREAM_HEADER = "X-WR-Upstream"

router = APIRouter()

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})   # a 2xx from one of these invalidates the path


def raw_path(request: Request) -> str:
	"""The path exactly as the client sent it (percent-encoding intact), without the query."""
	raw = request.scope.get("raw_path")
	return raw.split(b"?", 1)[0].decode("latin-1") if raw else request.url.path


def raw_query(request: Request) -> str:
	return request.scope.get("query_string", b"").decode("latin-1")


def build_response(
	status: int,
	headers: list[tuple[str, str]],
	body: bytes,
	x_cache: str,
	extra: list[tuple[str, str]] = (),
) -> Response:
	"""A Response that keeps repeated headers intact; Content-Length is computed from body."""
	response = Response(content=body, status_code=status)
	for name, value in [*headers, ("X-Cache", x_cache), *extra]:
		response.raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
	return response


def _text(status: int, message: str) -> Response:
	# uvicorn's own Date header is disabled (upstream's is forwarded), so generated responses add theirs.
	response = Response(message + "\n", status_code=status, media_type="text/plain")
	response.headers["Date"] = formatdate(usegmt=True)
	return response


@router.api_route("/{full_path:path}", methods=METHODS)
async def proxy(request: Request) -> Response:
	path = raw_path(request)
	if not path.startswith("/"):
		return _text(400, "the request target must be a path starting with '/'")
	if any(unquote(segment) in (".", "..") for segment in path.split("/")):
		return _text(400, "path must not contain '.' or '..' segments")
	first = path.split("/")[1] if path.startswith("/") else ""
	if first.startswith("_"):
		return _text(404, f"'/{first}' is reserved and never proxied")
	admitted = await _admit(request)
	if isinstance(admitted, Response):
		return admitted
	upstream, caller = admitted
	if request.method != "GET":
		return await _pass_through(request, upstream, path, caller)
	rule = request.app.state.rules.get().match(upstream.upstream_id, path)
	if (
		rule is None
		or rule.bypass
		or (rule.query_mode == "reject" and raw_query(request))
		or await request.body()   # the key ignores bodies, so a GET with one is never cached
	):
		return await _pass_through(request, upstream, path, caller)
	return await _cached_get(request, upstream, path, rule, caller)


@dataclass(frozen=True)
class _Caller:
	identity: db.Identity | None                                  # None = anonymous
	credential: tuple[str, str] | None = field(default=None, repr=False)   # (header, value) injected upstream; never logged


class RoutingRefusal(Exception):
	"""Why a request can't be routed, and the status it gets."""

	def __init__(self, status: int, message: str):
		super().__init__(message)
		self.status = status


def choose_upstream(conn, requested: str | None, identity: db.Identity | None) -> db.Upstream:
	"""The upstream X-WR-Upstream names (requested), or the only one identity is bound to.
	Raises RoutingRefusal: 400 for an unknown or ambiguous upstream, 403 for one the identity
	isn't bound to. Anonymous callers must always name the upstream."""
	def configured() -> str:
		return ", ".join(u.upstream_id for u in db.list_upstreams(conn)) or "(none)"

	if requested is not None:
		upstream = db.resolve_upstream(conn, requested)
		if upstream is None:
			raise RoutingRefusal(400, f"unknown upstream {requested[:64]!r}; configured upstreams: {configured()}")
		if identity is not None and upstream.upstream_id not in identity.upstream_ids:
			bound = ", ".join(identity.upstream_ids) or "nothing"
			raise RoutingRefusal(
				403, f"identity '{identity.name}' is not bound to upstream '{upstream.upstream_id}'; bound to: {bound}",
			)
		return upstream
	if identity is None:
		raise RoutingRefusal(400, f"send {UPSTREAM_HEADER} to choose an upstream; configured upstreams: {configured()}")
	if not identity.upstream_ids:
		raise RoutingRefusal(403, f"identity '{identity.name}' is not bound to any upstream")
	if len(identity.upstream_ids) > 1:
		raise RoutingRefusal(
			400,
			f"identity '{identity.name}' is bound to several upstreams; "
			f"send {UPSTREAM_HEADER} with one of: {', '.join(identity.upstream_ids)}",
		)
	upstream = db.get_upstream(conn, identity.upstream_ids[0])
	if upstream is None:   # deleted since the registry last loaded; its next refresh unbinds it
		raise RoutingRefusal(403, f"identity '{identity.name}' is not bound to any upstream")
	return upstream


def _single(request: Request, header: str) -> str | None | Response:
	"""The header's one value, stripped; None when absent; a 400 when repeated or empty."""
	values = request.headers.getlist(header)
	if not values:
		return None
	if len(values) > 1:
		return _text(400, f"send {header} once")
	value = values[0].strip()
	if not value:
		hint = "omit it for anonymous access" if header == HEADER else "omit it or name an upstream"
		return _text(400, f"{header} is empty; {hint}")
	return value


async def _admit(request: Request) -> tuple[db.Upstream, _Caller] | Response:
	"""Which upstream, who is calling, and whether they may. Runs before the cache, so HITs are gated too."""
	if "authorization" in request.headers:
		return _text(400, f"send {HEADER} instead of Authorization; Well RESTed injects upstream credentials itself")
	requested = _single(request, UPSTREAM_HEADER)
	name = _single(request, HEADER)
	for refusal in (requested, name):
		if isinstance(refusal, Response):
			return refusal
	identity = None
	if name is not None:
		registry = request.app.state.identities
		await registry.refresh()
		identity = registry.resolve(name)
		if identity is None:
			return _text(400, f"unknown identity {name[:64]!r}")
	try:
		upstream = choose_upstream(request.app.state.db, requested, identity)
	except RoutingRefusal as exc:
		log.debug("not routed; status=%d", exc.status)
		return _text(exc.status, str(exc))
	if identity is None:
		return upstream, _Caller(None)
	credential = registry.credential(identity)
	if credential is None:
		error = registry.load_error(identity)
		if error is not None:
			return _text(
				503,
				f"the Keychain read for identity '{identity.name}' failed ({error}); "
				"it is retried at the next identity change or proxy restart",
			)
		return _text(
			503,
			f"no token in the Keychain for identity '{identity.name}'; "
			f"set one with: python3 -m proxy identity rotate {identity.name} --expires 30d",
		)
	addr = client_addr(request.scope)
	if addr is None:
		return _text(403, "client address unavailable")
	verdict = await request.app.state.approvals.check(addr, identity, upstream)
	if verdict is Verdict.DENIED:
		return _text(403, f"{addr} is not approved to use identity '{identity.name}'")
	if verdict is Verdict.PENDING:
		response = _text(503, f"waiting for approval of {addr} to use identity '{identity.name}'; retry shortly")
		response.headers["Retry-After"] = "5"
		return response
	return upstream, _Caller(identity, credential)


def _relocated(request: Request, upstream: db.Upstream, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
	"""Headers for the client, with a Location at the upstream pointed back through this proxy.
	Applied at serve time, so each caller gets its own Host (already allowlisted by HostGuard)."""
	return rewrite_location(headers, upstream.base_url, str(request.base_url).rstrip("/"))


def _token_headers(request: Request, caller: _Caller, status: int | None = None) -> list[tuple[str, str]]:
	"""Record the token's status from an upstream reply, then warn if it needs attention."""
	if caller.identity is None:
		return []
	registry = request.app.state.identities
	identity = caller.identity if status is None else registry.record_status(caller.identity, status)
	text = registry.warning(identity)
	return [("Warning", text)] if text else []


def _partition(caller: _Caller) -> str:
	"""Every entry belongs to exactly one partition; nothing is ever served across partitions."""
	return caller.identity.cache_identity if caller.identity else ANONYMOUS


async def _fetch(request: Request, upstream: db.Upstream, path: str, caller: _Caller) -> httpx.Response:
	proxies = request.app.state.proxies
	await proxies.refresh()
	route = proxies.route(upstream.proxy)   # raises OutboundProxyFailure rather than going Direct
	headers = upstream_request_headers(request.headers.items())
	if caller.credential is not None:
		name = caller.credential[0].lower()   # the identity's header replaces any client header of that name
		headers = [(n, v) for n, v in headers if n.lower() != name]
		headers.append(caller.credential)
	return await request.app.state.upstream.send(
		upstream,
		request.method,
		path,
		raw_query(request),
		headers,
		await request.body(),
		route=route,
	)


def _upstream_error(upstream: db.Upstream, exc: httpx.HTTPError) -> Response:
	if isinstance(exc, OutboundProxyFailure):
		log.info("outbound proxy failed; upstream=%s proxy=%s status=%d", upstream.upstream_id, upstream.proxy, exc.status)
		return _text(exc.status, str(exc))
	status = 504 if isinstance(exc, httpx.TimeoutException) else 502
	log.debug("upstream failed; upstream=%s error=%s", upstream.upstream_id, type(exc).__name__)
	return _text(status, f"upstream '{upstream.upstream_id}' failed: {type(exc).__name__}")


async def _pass_through(request: Request, upstream: db.Upstream, path: str, caller: _Caller) -> Response:
	try:
		upstream_response = await _fetch(request, upstream, path, caller)
	except httpx.HTTPError as exc:
		return _upstream_error(upstream, exc)
	extra = _token_headers(request, caller, upstream_response.status_code)
	if request.method in MUTATING and 200 <= upstream_response.status_code < 300:
		purged = await request.app.state.cache.purge_by_path(upstream.upstream_id, path, _partition(caller))
		extra = [*extra, ("X-Cache-Invalidated", str(purged))]
	headers = _relocated(request, upstream, client_response_headers(decode_raw_headers(upstream_response.headers.raw)))
	response = build_response(upstream_response.status_code, headers, upstream_response.content, "PASS", extra)
	length = upstream_response.headers.get("content-length")
	if request.method == "HEAD" and length is not None:
		# a HEAD reply has no body, so Starlette computed 0; keep the upstream's length instead
		response.raw_headers = [(n, v) for n, v in response.raw_headers if n != b"content-length"]
		response.raw_headers.append((b"content-length", length.encode("latin-1")))
	return response


_STALE_WARNING = ("Warning", '110 - "response is stale"')


def _stale_response(request: Request, upstream: db.Upstream, caller: _Caller, entry: dict) -> Response:
	# No status is passed to _token_headers: on the transport-error path nothing came from
	# upstream, and on the final-5xx path record_status would ignore the status anyway.
	return build_response(
		entry["status"], _relocated(request, upstream, entry["headers"]), entry["body"], "STALE",
		[("Age", str(age_seconds(entry))), _STALE_WARNING, *_token_headers(request, caller)],
	)


async def _cached_get(request: Request, upstream: db.Upstream, path: str, rule: Rule, caller: _Caller) -> Response:
	cache = request.app.state.cache
	query = raw_query(request)
	identity = _partition(caller)
	key = build_key(identity, path, normalize_query(query, rule.query_mode))

	try:
		entry = await cache.get(upstream.upstream_id, key)   # lock held for this read only
	except Exception as exc:
		log.warning("cache read failed; upstream=%s error=%s", upstream.upstream_id, type(exc).__name__)
		entry = None
	now = time.time()
	if entry is not None and is_fresh(entry, now, ttl=rule.ttl_seconds):
		cache.record_hit(upstream.upstream_id, key, now)
		log.debug("hit; upstream=%s rule=%d", upstream.upstream_id, rule.id)
		return build_response(
			entry["status"], _relocated(request, upstream, entry["headers"]), entry["body"], "HIT",
			[("Age", str(age_seconds(entry, now))), *_token_headers(request, caller)],   # nothing came from upstream
		)

	async def fetch_and_store() -> tuple[int, list[tuple[str, str]], bytes]:
		upstream_response = await _fetch(request, upstream, path, caller)   # no lock held here
		headers = decode_raw_headers(upstream_response.headers.raw)
		body = upstream_response.content
		if upstream_response.status_code == 200 and len(body) <= request.app.state.settings.max_response_bytes:
			try:
				await cache.put(upstream.upstream_id, key, make_entry(   # lock held for this write only
					status=200,
					headers=cacheable_response_headers(headers),
					body=body,
					url=f"{path}?{query}" if query else path,
					identity=identity,
					rule_id=rule.id,
					ttl=rule.ttl_seconds,
					fetched_at=time.time(),
					hits=entry["hits"] if entry is not None else 0,   # a refresh keeps its popularity
				))
			except Exception as exc:
				log.warning("cache write failed; upstream=%s error=%s", upstream.upstream_id, type(exc).__name__)
			else:
				log.debug("stored; upstream=%s rule=%d bytes=%d", upstream.upstream_id, rule.id, len(body))
		return upstream_response.status_code, headers, body

	# One fetch per (upstream_id, key) at a time: concurrent callers share the leader's result,
	# but each labels it (MISS/REFRESH) and falls back to stale from its own read above.
	try:
		status, headers, body = await request.app.state.flights.do((upstream.upstream_id, key), fetch_and_store)
	except httpx.HTTPError as exc:
		if entry is not None:
			log.info("serving stale; upstream=%s rule=%d error=%s", upstream.upstream_id, rule.id, type(exc).__name__)
			return _stale_response(request, upstream, caller, entry)
		return _upstream_error(upstream, exc)
	if status in RETRYABLE_STATUSES and entry is not None:
		log.info("serving stale; upstream=%s rule=%d status=%d", upstream.upstream_id, rule.id, status)
		return _stale_response(request, upstream, caller, entry)
	x_cache = "REFRESH" if entry is not None else "MISS"
	extra = _token_headers(request, caller, status)
	return build_response(status, _relocated(request, upstream, client_response_headers(headers)), body, x_cache, extra)
