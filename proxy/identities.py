# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Explicit identities at runtime: the loading step, resolution, credential injection, token health.

Clients name an identity with X-WR-Identity; the proxy injects the upstream credential header
itself (Authorization, or the header a custom identity names). Tokens are read from the SecretStore (the Keychain) into memory at startup and
whenever identities_version changes, e.g. after a CLI rotation. A token is never logged,
never put in an error body, and never written anywhere but the SecretStore.
"""

import asyncio
import base64
import dataclasses
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from proxy import db
from proxy.keychain import SecretStore
from proxy.log import get_logger

log = get_logger(__name__, "IDENT")

HEADER = "X-WR-Identity"

_BAD_TOKEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")   # a custom header value may hold spaces, never a line break
_DURATION_RE = re.compile(r"(\d+)([dh])")


def parse_when(value: str, now: datetime) -> datetime:
	"""A token lifetime bound as typed: YYYY-MM-DD (00:00 UTC), an ISO timestamp with an offset, or 30d / 12h from now."""
	value = value.strip()
	match = _DURATION_RE.fullmatch(value)
	if match:
		amount = int(match[1])
		return now + (timedelta(days=amount) if match[2] == "d" else timedelta(hours=amount))
	try:
		parsed = datetime.fromisoformat(value)
	except ValueError:
		raise ValueError(f"bad time {value!r}; use YYYY-MM-DD, an ISO timestamp with an offset, or e.g. 30d") from None
	if len(value) == 10:   # a bare date: the start of that day, so we never overstate a token's life
		return parsed.replace(tzinfo=timezone.utc)
	if parsed.tzinfo is None:
		raise ValueError(f"timestamp {value!r} needs a UTC offset, e.g. +00:00")
	return parsed


@dataclass(frozen=True)
class Health:
	fraction: float   # 1.0 = freshly issued, 0.0 = expired
	level: str        # green | amber | red | expired | rejected


def health(identity: db.Identity, now: datetime) -> Health:
	lifetime = (identity.expires_at - identity.issued_at).total_seconds()
	remaining = (identity.expires_at - now).total_seconds()
	fraction = max(0.0, min(1.0, remaining / lifetime))
	if identity.last_status == "rejected":
		level = "rejected"
	elif remaining <= 0:
		level = "expired"
	elif fraction > 0.5:
		level = "green"
	elif fraction > 0.2:
		level = "amber"
	else:
		level = "red"
	return Health(fraction, level)


def warning(identity: db.Identity, now: datetime) -> str | None:
	"""A Warning header value once a token needs attention; None while it's healthy."""
	level = health(identity, now).level
	if level in ("green", "amber"):
		return None
	expires = f"{identity.expires_at.astimezone(timezone.utc):%Y-%m-%d}"
	if level == "rejected":
		text = f"token for '{identity.name}' was rejected by the upstream (401)"
	elif level == "expired":
		text = f"token for '{identity.name}' expired {expires}"
	else:
		text = f"token for '{identity.name}' expires {expires}"
	if identity.rotate_url:
		text += f"; rotate at {identity.rotate_url}"
	return f'299 - "{text}"'   # names and rotate_url are validated free of quotes and backslashes


def _error_name(exc: BaseException) -> str:
	"""The underlying error type (e.g. KeyringLocked behind a KeychainError); never the message."""
	return type(exc.__cause__ or exc).__name__


def validate_token(raw: str, scheme: str) -> str:
	"""A basic or bearer token has no whitespace at all. A custom token is the whole value of the
	identity's header (e.g. "Token abc123"), so it keeps inner spaces but no control characters (no
	CR/LF). Every scheme must fit a header, which httpx encodes as latin-1."""
	token = raw.strip()
	if scheme == "custom":
		if not token or _CONTROL_RE.search(token):
			raise ValueError("a custom header value must be non-empty, with no control characters or line breaks")
	elif not token or _BAD_TOKEN_RE.search(token):
		raise ValueError("a token must be non-empty, with no whitespace or control characters")
	try:
		token.encode("latin-1")
	except UnicodeEncodeError:
		raise ValueError("a token must only contain latin-1 characters; a header cannot carry anything else") from None
	return token


# --- add / rotate / delete, shared by the CLI and the admin UI ---------------------
#
# The Keychain-ordering rules live here so neither caller can drift from them. The
# Keychain calls run on a thread (they can block on a permission dialog); SQLite stays
# on the calling loop's thread.

def stored_identity(conn: sqlite3.Connection, typed: str) -> db.Identity:
	"""The identity as stored. Lookup ignores case, but the Keychain account is the stored spelling."""
	identity = db.get_identity(conn, typed)
	if identity is None:
		raise LookupError(f"no identity named {typed!r}")
	return identity


async def add_identity(
	conn: sqlite3.Connection,
	secrets: SecretStore,
	*,
	name: str,
	scheme: str,
	username: str | None,
	issued_at: datetime,
	expires_at: datetime,
	rotate_url: str | None,
	token: str,
	header: str | None = None,
) -> db.Identity:
	"""Validate everything, then Keychain first, then the row: a Keychain failure leaves no row.
	The identity starts bound to no upstream; bindings are made on the upstream.
	A custom identity that names no header sends Authorization, as every custom identity did before."""
	if scheme == "custom" and header is None:
		header = "Authorization"
	fields = dict(
		name=name, scheme=scheme, username=username, header=header,
		issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url,
	)
	db.check_identity(conn, **fields)   # nothing is touched while the fields are invalid
	token = validate_token(token, scheme)
	await asyncio.to_thread(secrets.set, name, token)
	try:
		return db.create_identity(conn, **fields)
	except BaseException:
		await asyncio.to_thread(secrets.delete, name)
		raise


async def rotate_token(
	conn: sqlite3.Connection,
	secrets: SecretStore,
	name: str,
	*,
	token: str,
	issued_at: datetime,
	expires_at: datetime,
	rotate_url: str | None = None,
) -> db.Identity:
	"""Keychain first, then the row (which clears last_status and bumps the version)."""
	identity = stored_identity(conn, name)
	stored = identity.name
	db.check_rotation(issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url)
	token = validate_token(token, identity.scheme)
	await asyncio.to_thread(secrets.set, stored, token)
	return db.rotate_identity(conn, stored, issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url)


async def remove_identity(conn: sqlite3.Connection, secrets: SecretStore, name: str) -> tuple[db.Identity, bool]:
	"""Keychain item first, then the row and its approvals. Returns (identity, whether an item existed).

	keyring reports "not found" and "locked or denied" the same way, so a failed delete is
	followed by a read: a token still present means nothing is deleted (RuntimeError, or the
	read's own KeychainError). The read happens only on that path, so a normal delete never
	needs decrypt permission.
	"""
	identity = stored_identity(conn, name)
	had_item = True
	if not await asyncio.to_thread(secrets.delete, identity.name):
		if await asyncio.to_thread(secrets.get, identity.name) is not None:
			raise RuntimeError(f"could not delete the Keychain item for '{identity.name}'; nothing was deleted")
		had_item = False
	db.delete_identity(conn, identity.name)
	return identity, had_item


class IdentityRegistry:
	def __init__(self, conn: sqlite3.Connection, secrets: SecretStore):
		self._conn = conn
		self._secrets = secrets
		self._version = -1
		self._by_name: dict[str, db.Identity] = {}
		self._tokens: dict[int, str] = {}   # identity id → token; never logged or repr'd
		self._load_errors: dict[int, str] = {}   # identity id → error type name of a failed Keychain read
		self._lock = asyncio.Lock()

	def __repr__(self) -> str:
		return f"IdentityRegistry(version={self._version}, identities={sorted(self._by_name)})"

	async def refresh(self) -> None:
		"""The loading step: reread identities and their tokens when identities_version moved."""
		if db.identities_version(self._conn) == self._version:
			return
		async with self._lock:
			version = db.identities_version(self._conn)
			if version == self._version:
				return
			identities = db.list_identities(self._conn)
			tokens: dict[int, str] = {}
			errors: dict[int, str] = {}
			for identity in identities:
				try:
					token = await asyncio.to_thread(self._secrets.get, identity.name)   # may raise a Keychain prompt
				except Exception as exc:
					# A locked or denied Keychain is a missing token for this identity, not a failed load.
					errors[identity.id] = _error_name(exc)
					log.warning("Keychain read failed; identity=%s error=%s", identity.name, errors[identity.id])
					continue
				if token:
					tokens[identity.id] = token
				else:
					log.warning("no token in the Keychain; identity=%s", identity.name)
			self._by_name = {identity.name: identity for identity in identities}
			self._tokens = tokens
			self._load_errors = errors
			self._version = version
			log.info(
				"identities loaded; count=%d missing_tokens=%d version=%d",
				len(identities), len(identities) - len(tokens), version,
			)

	def resolve(self, name: str) -> db.Identity | None:
		return self._by_name.get(name)

	def has_token(self, identity: db.Identity) -> bool:
		return identity.id in self._tokens

	def load_error(self, identity: db.Identity) -> str | None:
		"""The error type of this identity's failed Keychain read, or None if the read succeeded."""
		return self._load_errors.get(identity.id)

	def credential(self, identity: db.Identity) -> tuple[str, str] | None:
		"""The (header name, value) this identity injects upstream, or None when it has no token."""
		token = self._tokens.get(identity.id)
		if token is None:
			return None
		if identity.scheme == "bearer":
			return "Authorization", f"Bearer {token}"
		if identity.scheme == "custom":
			return identity.header, token   # the whole header value, verbatim, e.g. ("x-api-key", "sk-…")
		raw = f"{identity.username}:{token}".encode("utf-8")
		return "Authorization", "Basic " + base64.b64encode(raw).decode("ascii")

	def record_status(self, identity: db.Identity, status_code: int, now: datetime | None = None) -> db.Identity:
		"""401 marks the token rejected and 2xx marks it ok. Writes SQLite only on a change."""
		current = self._by_name.get(identity.name)
		if current is None or current.id != identity.id:
			return identity   # deleted or re-created meanwhile
		if status_code == 401:
			status = "rejected"
		elif 200 <= status_code < 300:
			status = "ok"
		else:
			return current
		if current.last_status == status:
			return current
		at = now or datetime.now(timezone.utc)
		db.set_identity_status(self._conn, current.id, status, at)
		updated = dataclasses.replace(current, last_status=status, last_status_at=at.replace(microsecond=0))
		self._by_name[current.name] = updated
		log.info("token status changed; identity=%s status=%s", current.name, status)
		return updated

	def warning(self, identity: db.Identity, now: datetime | None = None) -> str | None:
		return warning(identity, now or datetime.now(timezone.utc))
