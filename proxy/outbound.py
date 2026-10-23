# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbound proxies: an upstream can reach its base URL through one instead of connecting directly.

Well RESTed is itself a proxy, so code, logs and docs say *outbound proxy* wherever "proxy"
alone would be ambiguous. A proxy's password lives only in the Keychain (service
well-rested-proxy, account = proxy name): never in SQLite, a log line, an error body or a page.

add_proxy / edit_proxy / remove_proxy encode the Keychain-vs-row ordering, as identities.py does
for tokens. OutboundProxies is the loading step: rows plus passwords, reread whenever
proxies_version moves, after which on_reload lets the upstream client retire stale clients.
"""

import asyncio
import os
import re
import sqlite3
import ssl
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from proxy import db
from proxy.keychain import PROXY_SERVICE, SecretStore
from proxy.log import get_logger

log = get_logger(__name__, "OUTBND")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class OutboundProxyFailure(httpx.ProxyError):
	"""A request routed through an outbound proxy failed because of the proxy. The message names the
	proxy and its credential-free URL and is safe for a client error body; status is what the client
	gets. Being an httpx.HTTPError, it is served stale-if-error like any other upstream failure."""

	def __init__(self, message: str, status: int = 502):
		super().__init__(message)
		self.status = status


def normalize_url(raw: str) -> str:
	"""scheme://host[:port] for a proxy URL, or ValueError. httpx.Proxy decides which schemes exist.
	Errors never echo the URL: a rejected one may carry a password."""
	try:
		url = httpx.URL(raw.strip())
	except httpx.InvalidURL:
		raise ValueError("the proxy URL is not a valid URL") from None
	if url.userinfo:
		raise ValueError("the proxy URL must not contain credentials; put them in the Username/Password fields")
	try:
		httpx.Proxy(url)
	except ValueError:
		raise ValueError(f"unsupported proxy scheme {url.scheme!r}") from None
	if not url.host:
		raise ValueError("the proxy URL needs a host")
	if url.path not in ("", "/") or url.query or url.fragment:
		raise ValueError("the proxy URL must be scheme://host:port with no path or query")
	if url.scheme.startswith("socks"):
		if url.port is None:   # httpcore has no default SOCKS port
			raise ValueError("a SOCKS proxy URL needs a port, e.g. socks5://host:1080")
		try:
			import socksio  # noqa: F401
		except ImportError:
			raise ValueError("SOCKS proxies need socksio: python3 -m pip install --user 'httpx[socks]'") from None
	host = f"[{url.host}]" if ":" in url.host else url.host
	port = f":{url.port}" if url.port is not None else ""
	return f"{url.scheme}://{host}{port}"


def check_ca_bundle(raw: str | None) -> str | None:
	"""An absolute path to a loadable PEM CA bundle, or None for none."""
	if raw is None or not raw.strip():
		return None
	path = os.path.expanduser(raw.strip())
	if not os.path.isabs(path):
		raise ValueError(f"the CA bundle must be an absolute path: {raw.strip()!r}")
	try:
		ssl.create_default_context().load_verify_locations(cafile=path)
	except (OSError, ssl.SSLError) as exc:
		raise ValueError(f"cannot load CA bundle {path}: {type(exc).__name__}") from None
	return path


def tls_context(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
	"""httpx's default trust (certifi) plus ca_bundle; unverified when verify is off. trust_env=False:
	SSL_CERT_FILE / SSL_CERT_DIR from the launching shell are not consulted."""
	context = httpx.create_ssl_context(verify=verify, trust_env=False)
	if verify and ca_bundle:
		context.load_verify_locations(cafile=ca_bundle)
	return context


@dataclass(frozen=True)
class Route:
	"""One outbound proxy as the running proxy uses it. generation is proxies_version at load;
	pooled clients built for an older generation take no new requests."""
	name: str
	url: str
	username: str | None
	verify_tls: bool
	ca_bundle: str | None
	generation: int
	password: str | None = field(default=None, repr=False)   # from the Keychain; never logged

	@property
	def label(self) -> str:
		return f"outbound proxy '{self.name}' ({self.url})"

	def httpx_proxy(self) -> httpx.Proxy:
		auth = (self.username, self.password) if self.username and self.password is not None else None
		ssl_context = tls_context(self.verify_tls, self.ca_bundle) if self.url.startswith("https://") else None
		return httpx.Proxy(self.url, auth=auth, ssl_context=ssl_context)


# --- add / edit / remove, used by the admin UI ----------------------------------
#
# Keychain calls run on a thread (they can block on a permission dialog); SQLite stays on the loop.

def _validated(name: str, url: str, username: str | None, verify_tls: bool, ca_bundle: str | None) -> db.OutboundProxy:
	url = normalize_url(url)
	proxy = db.OutboundProxy(
		name=name,
		url=url,
		username=username or None,
		# Only an https:// proxy has a certificate. Others keep the default, so a later switch to
		# https:// doesn't start out unverified (the page's disabled checkbox submits nothing).
		verify_tls=verify_tls if url.startswith("https://") else True,
		ca_bundle=check_ca_bundle(ca_bundle),
	)
	db.check_proxy_fields(proxy.name, proxy.username)
	return proxy


def _password(raw: str, username: str | None) -> str | None:
	"""The new password, or None to keep (or have) none. Blank means unchanged."""
	if not raw:
		return None
	password = raw.strip()
	if not password or _CONTROL_RE.search(password):
		raise ValueError("a proxy password must be non-empty, without control characters")
	if not username:
		raise ValueError("a proxy password needs a username")
	return password


async def add_proxy(
	conn: sqlite3.Connection,
	secrets: SecretStore,
	*,
	name: str,
	url: str,
	username: str | None,
	password: str,
	verify_tls: bool = True,
	ca_bundle: str | None = None,
) -> db.OutboundProxy:
	"""Validate everything, then Keychain first, then the row: a Keychain failure leaves no row."""
	proxy = _validated(name, url, username, verify_tls, ca_bundle)
	if db.get_proxy(conn, name) is not None:
		raise ValueError(f"proxy {name!r} already exists")
	secret = _password(password, proxy.username)
	if secret is not None:
		await asyncio.to_thread(secrets.set, name, secret)
	try:
		created = db.create_proxy(conn, proxy)
	except BaseException:
		# Only if no row owns the name: a concurrent add of it may have written the same item.
		if secret is not None and db.get_proxy(conn, name) is None:
			try:
				await asyncio.to_thread(secrets.delete, name)
			except Exception as exc:   # never mask the original error
				log.warning("could not roll back the proxy password; proxy=%s error=%s", name, type(exc).__name__)
		raise
	log.info("proxy added; name=%s url=%s auth=%s", name, proxy.url, bool(proxy.username))
	return created


async def edit_proxy(
	conn: sqlite3.Connection,
	secrets: SecretStore,
	name: str,
	*,
	url: str,
	username: str | None,
	password: str,
	verify_tls: bool = True,
	ca_bundle: str | None = None,
) -> db.OutboundProxy:
	"""A blank password keeps the stored one. Clearing the username drops it from the Keychain."""
	current = db.get_proxy(conn, name)
	if current is None:
		raise LookupError(f"no proxy named {name!r}")
	proxy = _validated(name, url, username, verify_tls, ca_bundle)
	secret = _password(password, proxy.username)
	if secret is not None:
		await asyncio.to_thread(secrets.set, name, secret)
	updated = db.update_proxy(conn, proxy)
	if current.username and not proxy.username:
		try:
			await asyncio.to_thread(secrets.delete, name)
		except Exception as exc:   # a leftover item is harmless: nothing reads it without a username
			log.warning("could not drop the proxy password; proxy=%s error=%s", name, type(exc).__name__)
	log.info("proxy saved; name=%s url=%s auth=%s", name, proxy.url, bool(proxy.username))
	return updated


async def remove_proxy(conn: sqlite3.Connection, secrets: SecretStore, name: str) -> tuple[db.OutboundProxy, bool]:
	"""Refused while in use, before the Keychain is touched. Then the item, then the row.
	Returns (proxy, whether an item existed); a failed delete followed by a successful read
	means the Keychain refused, and nothing is deleted."""
	proxy = db.get_proxy(conn, name)
	if proxy is None:
		raise LookupError(f"no proxy named {name!r}")
	db.check_proxy_unused(conn, name)
	had_item = True
	if not await asyncio.to_thread(secrets.delete, name):
		if await asyncio.to_thread(secrets.get, name) is not None:
			raise RuntimeError(f"could not delete the Keychain item for proxy '{name}'; nothing was deleted")
		had_item = False
	db.delete_proxy(conn, name)
	log.info("proxy deleted; name=%s", name)
	return proxy, had_item


# --- the runtime registry ----------------------------------------------------------

class OutboundProxies:
	def __init__(
		self,
		conn: sqlite3.Connection,
		secrets: SecretStore,
		on_reload: Callable[[int], Awaitable[None]] | None = None,
	):
		self._conn = conn
		self._secrets = secrets
		self._on_reload = on_reload
		self._version = -1
		self._routes: dict[str, Route] = {}
		self._load_errors: dict[str, str] = {}   # name → error type of a failed Keychain read
		self._lock = asyncio.Lock()

	def __repr__(self) -> str:
		return f"OutboundProxies(version={self._version}, proxies={sorted(self._routes)})"

	async def refresh(self) -> None:
		"""The loading step: reread rows and passwords when proxies_version moved."""
		if db.proxies_version(self._conn) == self._version:
			return
		async with self._lock:
			version = db.proxies_version(self._conn)
			if version == self._version:
				return
			routes: dict[str, Route] = {}
			errors: dict[str, str] = {}
			for proxy in db.list_proxies(self._conn):
				password = None
				if proxy.username:
					try:
						password = await asyncio.to_thread(self._secrets.get, proxy.name)
					except Exception as exc:
						errors[proxy.name] = type(exc.__cause__ or exc).__name__
						log.warning("Keychain read failed; proxy=%s error=%s", proxy.name, errors[proxy.name])
					else:
						if password is None:
							log.warning("no password in the Keychain; proxy=%s", proxy.name)
				routes[proxy.name] = Route(
					proxy.name, proxy.url, proxy.username, proxy.verify_tls, proxy.ca_bundle, version, password,
				)
			self._routes, self._load_errors, self._version = routes, errors, version
			log.info("outbound proxies loaded; count=%d version=%d", len(routes), version)
			if self._on_reload is not None:
				await self._on_reload(version)

	def route(self, name: str | None) -> Route | None:
		"""The route for an upstream's proxy column; None means Direct. A proxy that can't be used
		raises OutboundProxyFailure (503) — it never falls back to a direct connection."""
		if name is None:
			return None
		route = self._routes.get(name)
		if route is None:
			raise OutboundProxyFailure(f"outbound proxy '{name}' is not loaded; retry shortly", 503)
		if route.username and route.password is None:
			error = self._load_errors.get(name)
			reason = f"has its password unavailable: the Keychain read failed ({error})" if error else "has no password in the Keychain"
			# The client gets the proxy and what's missing; who and where stay in the log.
			log.warning(
				"proxy unusable, 503; proxy=%s username=%s keychain_service=%s account=%s reason=%s",
				name, route.username, PROXY_SERVICE, name, error or "missing",
			)
			raise OutboundProxyFailure(f"{route.label} {reason}; set it on the Proxies page", 503)
		return route

	def password_state(self, name: str) -> str:
		"""For the Proxies page: 'none' (no username), 'stored', 'missing' or 'unreadable'."""
		route = self._routes.get(name)
		if route is None or not route.username:
			return "none"
		if name in self._load_errors:
			return "unreadable"
		return "stored" if route.password is not None else "missing"

	def load_error(self, name: str) -> str | None:
		return self._load_errors.get(name)
