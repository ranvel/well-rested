# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Header hygiene for both directions of the proxy.

Headers travel as lists of (name, value) str pairs so repeated headers (Set-Cookie,
Link, Vary) keep their order and multiplicity.
"""

from typing import Iterable
from urllib.parse import SplitResult, urlsplit

Headers = list[tuple[str, str]]

HOP_BY_HOP = frozenset({
	"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
	"te", "trailer", "transfer-encoding", "upgrade",
})

# Never forwarded upstream: httpx sets Host and Content-Length itself, and must choose
# Accept-Encoding so it can always decode the reply. Upstream credentials are injected
# by the proxy from X-WR-Identity, so a client's Authorization, Cookie and the identity
# header itself never travel upstream; nor does X-WR-Upstream, which only routes.
_REQUEST_DROP = frozenset({
	"host", "content-length", "accept-encoding", "authorization", "cookie", "x-wr-identity", "x-wr-upstream",
})

# Header names a custom identity may not inject under: everything the proxy or httpx owns on the
# way upstream. Authorization and Cookie are the point of a custom identity, so they stay allowed.
RESERVED_REQUEST_HEADERS = frozenset((HOP_BY_HOP | _REQUEST_DROP) - {"authorization", "cookie"})

# Never sent to the client as-is: httpx has already decoded the body, so the upstream's
# Content-Encoding and Content-Length no longer describe it (length is recomputed on
# serve). X-Cache belongs to this proxy. The upstream session (Set-Cookie) belongs to
# the proxy too: clients authenticate with X-WR-Identity, never with cookies.
_RESPONSE_DROP = frozenset({"content-encoding", "content-length", "x-cache", "set-cookie"})


def _strip(headers: Iterable[tuple[str, str]], drop: frozenset[str]) -> Headers:
	pairs = list(headers)
	named = set()   # headers listed in Connection: are hop-by-hop too
	for name, value in pairs:
		if name.lower() == "connection":
			named.update(token.strip().lower() for token in value.split(","))
	return [
		(name, value) for name, value in pairs
		if name.lower() not in HOP_BY_HOP and name.lower() not in drop and name.lower() not in named
	]


def upstream_request_headers(headers: Iterable[tuple[str, str]]) -> Headers:
	"""Client request headers safe to forward upstream; credentials are injected separately."""
	return _strip(headers, _REQUEST_DROP)


def client_response_headers(headers: Iterable[tuple[str, str]]) -> Headers:
	"""Upstream response headers for a live response to the client."""
	return _strip(headers, _RESPONSE_DROP)


def cacheable_response_headers(headers: Iterable[tuple[str, str]]) -> Headers:
	"""Client headers minus Set-Cookie and Age: a cached response must never replay a cookie
	or the upstream's frozen Age (the proxy computes its own on HIT)."""
	return [(n, v) for n, v in client_response_headers(headers) if n.lower() not in ("set-cookie", "age")]


def decode_raw_headers(raw: Iterable[tuple[bytes, bytes]]) -> Headers:
	"""bytes → str via latin-1, which round-trips every byte."""
	return [(name.decode("latin-1"), value.decode("latin-1")) for name, value in raw]


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: SplitResult, scheme: str) -> tuple[str, str | None, int | None]:
	"""(scheme, host, effective port); raises ValueError for a malformed port."""
	return scheme, url.hostname, url.port or _DEFAULT_PORTS.get(scheme)


def rewrite_location(headers: Headers, base_url: str, proxy_base: str) -> Headers:
	"""Point a Location at the upstream back through the proxy.

	An absolute (or scheme-relative) Location on the upstream's scheme, host and port, under
	base_url's path at a segment boundary, becomes proxy_base + the rest. A path-absolute
	Location under base_url's path loses that path, since clients send paths without it; one
	outside it is made absolute to the upstream, the only place it resolves. A rest that would
	read as scheme-relative ("//host", "/\\host") is always given proxy_base, never sent bare.
	Any other Location (another host, such as an SSO login) is left alone, and so is every body.
	"""
	base = urlsplit(base_url)
	base_path = base.path.rstrip("/")

	def under_base(path: str) -> str | None:
		"""The path below base_path, or None when the path isn't under it."""
		if path == base_path or path.startswith(base_path + "/"):
			return path[len(base_path):] or "/"
		return None

	def rewrite(value: str) -> str:
		try:
			loc = urlsplit(value)
			if loc.netloc:
				if _origin(loc, loc.scheme.lower() or base.scheme) != _origin(base, base.scheme):
					return value
				absolute = True
			elif value.startswith("/") and base_path:
				absolute = False
			else:
				return value
		except ValueError:
			return value
		rest = under_base(loc.path or "/")
		if rest is None:
			return value if absolute else f"{base.scheme}://{base.netloc}{value}"
		prefix = proxy_base if absolute or rest.startswith(("//", "/\\")) else ""
		query = f"?{loc.query}" if loc.query else ""
		fragment = f"#{loc.fragment}" if loc.fragment else ""
		return prefix + rest + query + fragment

	return [(name, rewrite(value) if name.lower() == "location" else value) for name, value in headers]
