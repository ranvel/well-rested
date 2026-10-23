# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admission that applies before any routing.

HostGuard rejects any Host outside the allowlist, which defeats DNS rebinding, including
from a browser on an already-approved machine. It also keeps reserved /_… paths (the
admin UI) loopback-only, so a LAN client can never reach the pages that would approve
it. Client addresses come from the socket only; X-Forwarded-For is never consulted.
"""

import ipaddress
from email.utils import formatdate

from proxy.log import get_logger

log = get_logger(__name__, "ADMIT")

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _is_port(text: str) -> bool:
	return text.isascii() and text.isdigit()   # str.isdigit alone accepts superscripts like "\xb2"


def normalize_host(value: str) -> str | None:
	"""A Host header (or allowlist entry) as a comparable name without port; None if malformed."""
	value = value.strip().lower()
	if value.startswith("["):
		end = value.find("]")
		if end == -1:
			return None
		host, rest = value[:end + 1], value[end + 1:]
		if rest and not (rest.startswith(":") and _is_port(rest[1:])):
			return None
		return host
	host, sep, port = value.partition(":")
	if sep and not _is_port(port):
		return None   # also refuses an unbracketed IPv6 literal
	host = host.rstrip(".")
	return host or None


def parse_allowed_hosts(spec: str) -> frozenset[str]:
	hosts = set()
	for entry in spec.split(","):
		if not entry.strip():
			continue
		host = normalize_host(entry)
		if host is None:
			raise ValueError(f"bad PROXY_ALLOWED_HOSTS entry {entry.strip()!r}")
		hosts.add(host)
	if not hosts:
		raise ValueError("PROXY_ALLOWED_HOSTS must name at least one host")
	return frozenset(hosts)


def is_loopback(addr: str | None) -> bool:
	if not addr:
		return False
	try:
		ip = ipaddress.ip_address(addr)
	except ValueError:
		return False
	if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
		ip = ip.ipv4_mapped
	return ip.is_loopback


def client_addr(scope) -> str | None:
	client = scope.get("client")
	return client[0] if client else None


def _is_reserved(path: str) -> bool:
	segments = path.split("/")
	return len(segments) > 1 and segments[1].startswith("_")


async def _reject(send, status: int, message: str) -> None:
	body = (message + "\n").encode("utf-8")
	await send({
		"type": "http.response.start",
		"status": status,
		"headers": [
			(b"content-type", b"text/plain; charset=utf-8"),
			(b"content-length", str(len(body)).encode("latin-1")),
			(b"date", formatdate(usegmt=True).encode("latin-1")),
		],
	})
	await send({"type": "http.response.body", "body": body})


class HostGuard:
	def __init__(self, app, allowed_hosts: frozenset[str]):
		self.app = app
		self.allowed_hosts = allowed_hosts

	async def __call__(self, scope, receive, send):
		if scope["type"] != "http":
			await self.app(scope, receive, send)
			return
		host = next((normalize_host(v.decode("latin-1")) for n, v in scope["headers"] if n == b"host"), None)
		if host is None or host not in self.allowed_hosts:
			log.debug("host refused; host=%s client=%s", host, client_addr(scope))
			await _reject(send, 400, "Host not allowed; add it to PROXY_ALLOWED_HOSTS")
			return
		if _is_reserved(scope.get("path", "")) and not is_loopback(client_addr(scope)):
			log.debug("reserved path refused; client=%s", client_addr(scope))
			await _reject(send, 403, "reserved paths answer loopback clients only")
			return
		await self.app(scope, receive, send)
