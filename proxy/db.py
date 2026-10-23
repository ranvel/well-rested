# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite persistence: versioned schema, upstream routes, cache rules, and the version counters.

The connection runs in autocommit mode; multi-statement writes use _transaction().
Tokens are never stored here; they live in the Keychain (see proxy.keychain).
"""

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from proxy.headers import RESERVED_REQUEST_HEADERS
from proxy.log import get_logger
from proxy.rules import QUERY_MODES, Rule, RuleSet, compile_pattern

log = get_logger(__name__, "SQLDB")

# Upstream ids name shelf files and travel in X-WR-Upstream: one header token, no "/".
UPSTREAM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

SCHEMA_VERSION = 9

# Entry i migrates the schema from version i to i + 1. Never edit a shipped entry; append a new one.
_MIGRATIONS: list[tuple[str, ...]] = [
	(	# 0 → 1: Plan 1's schema
		"""CREATE TABLE upstreams (
			prefix      TEXT PRIMARY KEY,
			base_url    TEXT NOT NULL,
			auth_header TEXT,
			verify_tls  INTEGER NOT NULL DEFAULT 1,
			timeout_s   REAL NOT NULL DEFAULT 30.0
		)""",
		"""CREATE TABLE rules (
			id          INTEGER PRIMARY KEY AUTOINCREMENT,
			pattern     TEXT NOT NULL UNIQUE,
			ttl_seconds INTEGER,
			query_mode  TEXT NOT NULL DEFAULT 'include'
			            CHECK (query_mode IN ('include', 'ignore', 'reject')),
			shared      INTEGER NOT NULL DEFAULT 0,
			enabled     INTEGER NOT NULL DEFAULT 1,
			note        TEXT,
			created_at  TEXT NOT NULL,
			updated_at  TEXT NOT NULL
		)""",
		"CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
		"INSERT INTO meta (key, value) VALUES ('rules_version', 0)",
	),
	(	# 1 → 2: explicit identities (Plan 1.5)
		"ALTER TABLE upstreams DROP COLUMN auth_header",
		"""CREATE TABLE identities (
			id              INTEGER PRIMARY KEY AUTOINCREMENT,
			name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
			prefix          TEXT NOT NULL REFERENCES upstreams(prefix),
			scheme          TEXT NOT NULL CHECK (scheme IN ('basic', 'bearer')),
			username        TEXT,
			issued_at       TEXT NOT NULL,
			expires_at      TEXT NOT NULL,
			rotate_url      TEXT,
			last_status     TEXT CHECK (last_status IN ('ok', 'rejected')),
			last_status_at  TEXT,
			created_at      TEXT NOT NULL,
			updated_at      TEXT NOT NULL
		)""",
		"""CREATE TABLE approvals (
			client_addr  TEXT NOT NULL,
			identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
			granted_at   TEXT NOT NULL,
			PRIMARY KEY (client_addr, identity_id)
		)""",
		"INSERT INTO meta (key, value) VALUES ('identities_version', 0)",
	),
	(	# 2 → 3: no shared cache entries (spec amendment 2026-10-11)
		"ALTER TABLE rules DROP COLUMN shared",
	),
	(	# 3 → 4: an identity binds to any number of upstreams (spec amendment 2026-10-12)
		# identities.prefix carries an FK, so the table is rebuilt. foreign_keys is ON and can't be
		# switched off inside the migration's transaction, so approvals are set aside first: dropping
		# the old identities table would otherwise cascade-delete them.
		"CREATE TABLE _approvals_v3 AS SELECT client_addr, identity_id, granted_at FROM approvals",
		"DROP TABLE approvals",
		"ALTER TABLE identities RENAME TO _identities_v3",
		"""CREATE TABLE identities (
			id              INTEGER PRIMARY KEY AUTOINCREMENT,
			name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
			scheme          TEXT NOT NULL CHECK (scheme IN ('basic', 'bearer')),
			username        TEXT,
			issued_at       TEXT NOT NULL,
			expires_at      TEXT NOT NULL,
			rotate_url      TEXT,
			last_status     TEXT CHECK (last_status IN ('ok', 'rejected')),
			last_status_at  TEXT,
			created_at      TEXT NOT NULL,
			updated_at      TEXT NOT NULL
		)""",
		"INSERT INTO identities (id, name, scheme, username, issued_at, expires_at, rotate_url, "
		"last_status, last_status_at, created_at, updated_at) "
		"SELECT id, name, scheme, username, issued_at, expires_at, rotate_url, "
		"last_status, last_status_at, created_at, updated_at FROM _identities_v3",
		# Ids are part of the cache partition and are never reused, so keep the high-water mark.
		"DELETE FROM sqlite_sequence WHERE name = 'identities'",
		"INSERT INTO sqlite_sequence (name, seq) SELECT 'identities', seq FROM sqlite_sequence WHERE name = '_identities_v3'",
		"""CREATE TABLE identity_upstreams (
			identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
			prefix       TEXT NOT NULL REFERENCES upstreams(prefix) ON DELETE CASCADE,
			PRIMARY KEY (identity_id, prefix)
		)""",
		"INSERT INTO identity_upstreams (identity_id, prefix) SELECT id, prefix FROM _identities_v3",
		"DROP TABLE _identities_v3",
		"""CREATE TABLE approvals (
			client_addr  TEXT NOT NULL,
			identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
			granted_at   TEXT NOT NULL,
			PRIMARY KEY (client_addr, identity_id)
		)""",
		"INSERT INTO approvals (client_addr, identity_id, granted_at) SELECT client_addr, identity_id, granted_at FROM _approvals_v3",
		"DROP TABLE _approvals_v3",
	),
	(	# 4 → 5: outbound proxies (spec amendment 2026-10-13)
		"""CREATE TABLE proxies (
			name        TEXT PRIMARY KEY,
			url         TEXT NOT NULL,
			username    TEXT,
			verify_tls  INTEGER NOT NULL DEFAULT 1,
			ca_bundle   TEXT,
			created_at  TEXT NOT NULL,
			updated_at  TEXT NOT NULL
		)""",
		# ADD COLUMN may carry REFERENCES under foreign_keys=ON because its default is NULL (= Direct).
		"ALTER TABLE upstreams ADD COLUMN proxy TEXT REFERENCES proxies(name) ON DELETE RESTRICT",
		"INSERT INTO meta (key, value) VALUES ('proxies_version', 0)",
	),
	(	# 5 → 6: a custom scheme, whose token is the whole Authorization value (spec amendment 2026-10-13)
		# The scheme CHECK changes only by rebuilding identities. Both of its children cascade off it under
		# foreign_keys=ON, so bindings and approvals are set aside first, as in 3 → 4.
		"CREATE TABLE _bindings_v5 AS SELECT identity_id, prefix FROM identity_upstreams",
		"CREATE TABLE _approvals_v5 AS SELECT client_addr, identity_id, granted_at FROM approvals",
		"DROP TABLE identity_upstreams",
		"DROP TABLE approvals",
		"ALTER TABLE identities RENAME TO _identities_v5",
		"""CREATE TABLE identities (
			id              INTEGER PRIMARY KEY AUTOINCREMENT,
			name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
			scheme          TEXT NOT NULL CHECK (scheme IN ('basic', 'bearer', 'custom')),
			username        TEXT,
			issued_at       TEXT NOT NULL,
			expires_at      TEXT NOT NULL,
			rotate_url      TEXT,
			last_status     TEXT CHECK (last_status IN ('ok', 'rejected')),
			last_status_at  TEXT,
			created_at      TEXT NOT NULL,
			updated_at      TEXT NOT NULL
		)""",
		"INSERT INTO identities (id, name, scheme, username, issued_at, expires_at, rotate_url, "
		"last_status, last_status_at, created_at, updated_at) "
		"SELECT id, name, scheme, username, issued_at, expires_at, rotate_url, "
		"last_status, last_status_at, created_at, updated_at FROM _identities_v5",
		# Ids are part of the cache partition and are never reused, so keep the high-water mark.
		"DELETE FROM sqlite_sequence WHERE name = 'identities'",
		"INSERT INTO sqlite_sequence (name, seq) SELECT 'identities', seq FROM sqlite_sequence WHERE name = '_identities_v5'",
		"DROP TABLE _identities_v5",
		"""CREATE TABLE identity_upstreams (
			identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
			prefix       TEXT NOT NULL REFERENCES upstreams(prefix) ON DELETE CASCADE,
			PRIMARY KEY (identity_id, prefix)
		)""",
		"INSERT INTO identity_upstreams (identity_id, prefix) SELECT identity_id, prefix FROM _bindings_v5",
		"DROP TABLE _bindings_v5",
		"""CREATE TABLE approvals (
			client_addr  TEXT NOT NULL,
			identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
			granted_at   TEXT NOT NULL,
			PRIMARY KEY (client_addr, identity_id)
		)""",
		"INSERT INTO approvals (client_addr, identity_id, granted_at) SELECT client_addr, identity_id, granted_at FROM _approvals_v5",
		"DROP TABLE _approvals_v5",
	),
	(	# 6 → 7: an upstream is named by its upstream_id, no longer a path prefix (spec amendment 2026-10-14)
		# RENAME COLUMN also rewrites the REFERENCES clauses that name it, so nothing is rebuilt.
		"ALTER TABLE upstreams RENAME COLUMN prefix TO upstream_id",
		"ALTER TABLE identity_upstreams RENAME COLUMN prefix TO upstream_id",
	),
	(	# 7 → 8: every rule belongs to one upstream, and its pattern is that upstream's path (spec amendment 2026-10-14)
		# A pattern that began with a literal "/<upstream_id>" loses that segment and joins the upstream; any other
		# pattern can't be placed and is dropped. Ids and the AUTOINCREMENT high-water mark are kept, because cache
		# entries carry rule_id. "/jira" and "/jira/" both become "/"; the older rule wins.
		"ALTER TABLE rules RENAME TO _rules_v7",
		"""CREATE TABLE rules (
			id          INTEGER PRIMARY KEY AUTOINCREMENT,
			upstream_id TEXT NOT NULL REFERENCES upstreams(upstream_id) ON DELETE CASCADE,
			pattern     TEXT NOT NULL,
			ttl_seconds INTEGER,
			query_mode  TEXT NOT NULL DEFAULT 'include'
			            CHECK (query_mode IN ('include', 'ignore', 'reject')),
			enabled     INTEGER NOT NULL DEFAULT 1,
			note        TEXT,
			created_at  TEXT NOT NULL,
			updated_at  TEXT NOT NULL,
			UNIQUE (upstream_id, pattern)
		)""",
		# substr, not LIKE: "_" is a LIKE wildcard and LIKE ignores case.
		"INSERT OR IGNORE INTO rules (id, upstream_id, pattern, ttl_seconds, query_mode, enabled, note, created_at, updated_at) "
		"SELECT r.id, u.upstream_id, "
		"CASE WHEN r.pattern = '/' || u.upstream_id THEN '/' ELSE substr(r.pattern, length(u.upstream_id) + 2) END, "
		"r.ttl_seconds, r.query_mode, r.enabled, r.note, r.created_at, r.updated_at "
		"FROM _rules_v7 r JOIN upstreams u ON r.pattern = '/' || u.upstream_id "
		"OR substr(r.pattern, 1, length(u.upstream_id) + 2) = '/' || u.upstream_id || '/' "
		"ORDER BY r.id",
		"DELETE FROM sqlite_sequence WHERE name = 'rules'",
		"INSERT INTO sqlite_sequence (name, seq) SELECT 'rules', seq FROM sqlite_sequence WHERE name = '_rules_v7'",
		"DROP TABLE _rules_v7",
	),
	(	# 8 → 9: a custom identity names the header it injects under (spec amendment 2026-10-16)
		# NULL for basic and bearer, which are always Authorization. Existing custom identities keep sending
		# Authorization, which is what they sent before the column existed.
		"ALTER TABLE identities ADD COLUMN header TEXT",
		"UPDATE identities SET header = 'Authorization' WHERE scheme = 'custom'",
	),
]


@dataclass(frozen=True)
class Upstream:
	upstream_id: str
	base_url: str
	verify_tls: bool = True
	timeout_s: float = 30.0
	proxy: str | None = None   # outbound proxy name; None = Direct


def connect(path: Path | str) -> sqlite3.Connection:
	"""Open the database in WAL mode with foreign keys on, migrating it to SCHEMA_VERSION."""
	if sqlite3.sqlite_version_info < (3, 35, 0):
		raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is too old; 3.35 or newer is required")
	conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA journal_mode=WAL")
	conn.execute("PRAGMA foreign_keys=ON")
	_migrate(conn)
	log.debug("database ready; path=%s", path)
	return conn


def _schema_version(conn: sqlite3.Connection) -> int:
	version = conn.execute("PRAGMA user_version").fetchone()[0]
	if version == 0 and conn.execute(
		"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'upstreams'"
	).fetchone():
		return 1   # created by Plan 1, before the schema was versioned
	return version


def _migrate(conn: sqlite3.Connection) -> None:
	# BEGIN IMMEDIATE: a second process (the CLI) waits here, then sees the migrated version.
	with _transaction(conn):
		version = _schema_version(conn)
		if version > SCHEMA_VERSION:
			raise RuntimeError(f"proxy.db is schema v{version}; this code knows up to v{SCHEMA_VERSION}")
		for target in range(version + 1, SCHEMA_VERSION + 1):
			for statement in _MIGRATIONS[target - 1]:
				conn.execute(statement)
		conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
	if version < SCHEMA_VERSION:
		log.info("schema migrated; from=%d to=%d", version, SCHEMA_VERSION)


def identities_version(conn: sqlite3.Connection) -> int:
	return conn.execute("SELECT value FROM meta WHERE key = 'identities_version'").fetchone()[0]


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
	conn.execute("BEGIN IMMEDIATE")
	try:
		yield
	except BaseException:
		conn.execute("ROLLBACK")
		raise
	conn.execute("COMMIT")


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- upstreams ---------------------------------------------------------------

def _row_to_upstream(row: sqlite3.Row) -> Upstream:
	return Upstream(
		upstream_id=row["upstream_id"],
		base_url=row["base_url"],
		verify_tls=bool(row["verify_tls"]),
		timeout_s=row["timeout_s"],
		proxy=row["proxy"],
	)


def _validate_upstream(upstream_id: str, base_url: str) -> str:
	"""Raise ValueError for a bad upstream id or base_url; return the normalized base_url."""
	if not UPSTREAM_ID_RE.fullmatch(upstream_id):
		raise ValueError(
			f"invalid upstream id {upstream_id!r}; use letters, digits, '.', '_' or '-', "
			"starting with a letter or digit"
		)
	normalized = base_url.rstrip("/")
	if not normalized.startswith(("http://", "https://")):
		raise ValueError(f"base_url must be http(s): {base_url!r}")
	return normalized


def put_upstream(conn: sqlite3.Connection, upstream: Upstream, identities: Iterable[str] | None = None) -> Upstream:
	"""Insert or update an upstream route (an UPSERT, so its bindings survive). With identities,
	its bindings are replaced in the same transaction; None leaves them unchanged."""
	base_url = _validate_upstream(upstream.upstream_id, upstream.base_url)
	clash = conn.execute(
		"SELECT upstream_id FROM upstreams WHERE upstream_id != ? AND lower(upstream_id) = lower(?)",
		(upstream.upstream_id, upstream.upstream_id),
	).fetchone()
	if clash:
		raise ValueError(
			f"upstream id {upstream.upstream_id!r} collides with existing {clash['upstream_id']!r} "
			"(shelf filenames are case-insensitive on macOS)"
		)
	if upstream.proxy is not None and get_proxy(conn, upstream.proxy) is None:
		raise ValueError(f"no outbound proxy named {upstream.proxy!r}")
	with _transaction(conn):
		conn.execute(
			"INSERT INTO upstreams (upstream_id, base_url, verify_tls, timeout_s, proxy) VALUES (?, ?, ?, ?, ?) "
			"ON CONFLICT(upstream_id) DO UPDATE SET base_url = excluded.base_url, "
			"verify_tls = excluded.verify_tls, timeout_s = excluded.timeout_s, proxy = excluded.proxy",
			(upstream.upstream_id, base_url, int(upstream.verify_tls), upstream.timeout_s, upstream.proxy),
		)
		if identities is not None:
			_replace_bindings(conn, upstream.upstream_id, identities)
	log.debug("upstream saved; upstream=%s base_url=%s", upstream.upstream_id, base_url)
	return get_upstream(conn, upstream.upstream_id)


def get_upstream(conn: sqlite3.Connection, upstream_id: str) -> Upstream | None:
	row = conn.execute("SELECT * FROM upstreams WHERE upstream_id = ?", (upstream_id,)).fetchone()
	return _row_to_upstream(row) if row else None


def resolve_upstream(conn: sqlite3.Connection, upstream_id: str) -> Upstream | None:
	"""The upstream an X-WR-Upstream value names, in any letter case (ids are unique case-insensitively;
	should two differ only in case anyway, the exact spelling wins)."""
	row = conn.execute(
		"SELECT * FROM upstreams WHERE lower(upstream_id) = lower(?) ORDER BY upstream_id = ? DESC, upstream_id LIMIT 1",
		(upstream_id, upstream_id),
	).fetchone()
	return _row_to_upstream(row) if row else None


def list_upstreams(conn: sqlite3.Connection) -> list[Upstream]:
	return [_row_to_upstream(r) for r in conn.execute("SELECT * FROM upstreams ORDER BY upstream_id")]


def delete_upstream(conn: sqlite3.Connection, upstream_id: str) -> bool:
	"""Delete an upstream route and its rules. Its identities are unbound (the identities themselves stay)."""
	with _transaction(conn):
		bound = conn.execute("SELECT COUNT(*) FROM identity_upstreams WHERE upstream_id = ?", (upstream_id,)).fetchone()[0]
		rules = conn.execute("SELECT COUNT(*) FROM rules WHERE upstream_id = ?", (upstream_id,)).fetchone()[0]
		cur = conn.execute("DELETE FROM upstreams WHERE upstream_id = ?", (upstream_id,))   # bindings and rules go by ON DELETE CASCADE
		if bound:
			_bump_identities_version(conn)
		if rules:
			_bump_rules_version(conn)
	if cur.rowcount:
		log.debug("upstream deleted; upstream=%s unbound=%d rules=%d", upstream_id, bound, rules)
	return bool(cur.rowcount)


def identities_by_upstream(conn: sqlite3.Connection) -> dict[str, list[str]]:
	"""Bound identity names per upstream id, sorted; upstreams with none are absent."""
	rows = conn.execute(
		"SELECT b.upstream_id, i.name FROM identity_upstreams b JOIN identities i ON i.id = b.identity_id "
		"ORDER BY b.upstream_id, i.name"
	)
	result: dict[str, list[str]] = {}
	for r in rows:
		result.setdefault(r["upstream_id"], []).append(r["name"])
	return result


def set_upstream_identities(conn: sqlite3.Connection, upstream_id: str, names: Iterable[str]) -> None:
	"""Replace the set of identities bound to an upstream. Names match ignoring case."""
	if get_upstream(conn, upstream_id) is None:
		raise ValueError(f"no upstream {upstream_id!r}")
	with _transaction(conn):
		_replace_bindings(conn, upstream_id, names)


def _replace_bindings(conn: sqlite3.Connection, upstream_id: str, names: Iterable[str]) -> None:
	"""Call inside a transaction. Bumps identities_version only on a real change, because every
	bump makes the running proxy reread all tokens from the Keychain."""
	wanted: set[int] = set()
	unknown: list[str] = []
	for name in names:
		row = conn.execute("SELECT id FROM identities WHERE name = ?", (name,)).fetchone()   # column is NOCASE
		if row is None:
			unknown.append(name)
		else:
			wanted.add(row["id"])
	if unknown:
		raise ValueError(f"no identity named {', '.join(repr(n) for n in unknown)}")
	current = {r["identity_id"] for r in conn.execute(
		"SELECT identity_id FROM identity_upstreams WHERE upstream_id = ?", (upstream_id,),
	)}
	if wanted == current:
		return
	conn.executemany(
		"DELETE FROM identity_upstreams WHERE identity_id = ? AND upstream_id = ?", [(i, upstream_id) for i in current - wanted],
	)
	conn.executemany(
		"INSERT INTO identity_upstreams (identity_id, upstream_id) VALUES (?, ?)", [(i, upstream_id) for i in wanted - current],
	)
	_bump_identities_version(conn)
	log.debug("bindings changed; upstream=%s identities=%d", upstream_id, len(wanted))


def seed_upstreams(conn: sqlite3.Connection, pairs: Iterable[tuple[str, str]]) -> int:
	"""Seed upstreams on first run (empty table only), all-or-nothing. Returns how many were inserted."""
	if conn.execute("SELECT COUNT(*) FROM upstreams").fetchone()[0]:
		return 0
	validated = [(upstream_id, _validate_upstream(upstream_id, base_url)) for upstream_id, base_url in pairs]
	inserted = 0
	seen: set[str] = set()
	with _transaction(conn):
		for upstream_id, base_url in validated:
			if upstream_id.lower() in seen:   # a duplicate, or a case variant that would share its shelf file
				log.warning("seed entry skipped; upstream=%s reason=duplicate id", upstream_id)
				continue
			seen.add(upstream_id.lower())
			cur = conn.execute("INSERT INTO upstreams (upstream_id, base_url) VALUES (?, ?)", (upstream_id, base_url))
			inserted += cur.rowcount
	if inserted:
		log.info("upstreams seeded from env; count=%d", inserted)
	return inserted


# --- outbound proxies -------------------------------------------------------------
#
# A proxy's password lives in the Keychain (service well-rested-proxy, account = name), never here.
# URLs are validated by proxy.outbound.normalize_url before they reach these functions.

_PROXY_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@dataclass(frozen=True)
class OutboundProxy:
	name: str                      # immutable: the Keychain account and the upstreams' foreign key
	url: str                       # scheme://host[:port], never userinfo
	username: str | None = None    # None = no proxy auth
	verify_tls: bool = True        # https:// proxies only: verify the proxy's own certificate
	ca_bundle: str | None = None   # PEM trusted for the proxy and every upstream routed through it


def _row_to_proxy(row: sqlite3.Row) -> OutboundProxy:
	return OutboundProxy(
		name=row["name"],
		url=row["url"],
		username=row["username"],
		verify_tls=bool(row["verify_tls"]),
		ca_bundle=row["ca_bundle"],
	)


def check_proxy_fields(name: str, username: str | None) -> None:
	"""Raise ValueError for a bad name or username."""
	if not _PROXY_NAME_RE.fullmatch(name):
		raise ValueError(
			f"invalid proxy name {name!r}; use up to 64 lowercase letters, digits or '-', "
			"starting with a letter or digit"
		)
	if username is not None and (not username or ":" in username or _CONTROL_RE.search(username)):
		raise ValueError("a proxy username must be non-empty, without ':' or control characters")


def proxies_version(conn: sqlite3.Connection) -> int:
	return conn.execute("SELECT value FROM meta WHERE key = 'proxies_version'").fetchone()[0]


def _bump_proxies_version(conn: sqlite3.Connection) -> None:
	conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'proxies_version'")


def create_proxy(conn: sqlite3.Connection, proxy: OutboundProxy) -> OutboundProxy:
	check_proxy_fields(proxy.name, proxy.username)
	if get_proxy(conn, proxy.name) is not None:
		raise ValueError(f"proxy {proxy.name!r} already exists")
	now = _now()
	with _transaction(conn):
		conn.execute(
			"INSERT INTO proxies (name, url, username, verify_tls, ca_bundle, created_at, updated_at) "
			"VALUES (?, ?, ?, ?, ?, ?, ?)",
			(proxy.name, proxy.url, proxy.username, int(proxy.verify_tls), proxy.ca_bundle, now, now),
		)
		_bump_proxies_version(conn)
	log.debug("proxy created; name=%s", proxy.name)
	return get_proxy(conn, proxy.name)


def update_proxy(conn: sqlite3.Connection, proxy: OutboundProxy) -> OutboundProxy:
	"""Replace a proxy's fields; the name is immutable."""
	check_proxy_fields(proxy.name, proxy.username)
	with _transaction(conn):
		cur = conn.execute(
			"UPDATE proxies SET url = ?, username = ?, verify_tls = ?, ca_bundle = ?, updated_at = ? WHERE name = ?",
			(proxy.url, proxy.username, int(proxy.verify_tls), proxy.ca_bundle, _now(), proxy.name),
		)
		if cur.rowcount == 0:
			raise LookupError(f"no proxy named {proxy.name!r}")
		_bump_proxies_version(conn)
	log.debug("proxy updated; name=%s", proxy.name)
	return get_proxy(conn, proxy.name)


def get_proxy(conn: sqlite3.Connection, name: str) -> OutboundProxy | None:
	row = conn.execute("SELECT * FROM proxies WHERE name = ?", (name,)).fetchone()
	return _row_to_proxy(row) if row else None


def list_proxies(conn: sqlite3.Connection) -> list[OutboundProxy]:
	return [_row_to_proxy(r) for r in conn.execute("SELECT * FROM proxies ORDER BY name")]


def upstreams_by_proxy(conn: sqlite3.Connection) -> dict[str, list[str]]:
	"""Upstream ids routed through each proxy, sorted; unused proxies are absent."""
	result: dict[str, list[str]] = {}
	for r in conn.execute("SELECT proxy, upstream_id FROM upstreams WHERE proxy IS NOT NULL ORDER BY upstream_id"):
		result.setdefault(r["proxy"], []).append(r["upstream_id"])
	return result


def check_proxy_unused(conn: sqlite3.Connection, name: str) -> None:
	"""ValueError naming the upstreams while any upstream routes through this proxy."""
	users = upstreams_by_proxy(conn).get(name)
	if users:
		raise ValueError(
			f"proxy {name!r} is used by {', '.join('/' + p for p in users)}; point them elsewhere first"
		)


def delete_proxy(conn: sqlite3.Connection, name: str) -> bool:
	"""Refused while in use (the FK's ON DELETE RESTRICT is the backstop)."""
	check_proxy_unused(conn, name)
	with _transaction(conn):
		cur = conn.execute("DELETE FROM proxies WHERE name = ?", (name,))
		if cur.rowcount:
			_bump_proxies_version(conn)
	if cur.rowcount:
		log.debug("proxy deleted; name=%s", name)
	return bool(cur.rowcount)


# --- identities --------------------------------------------------------------

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")   # an RFC 7230 token
RESERVED_IDENTITY_NAMES = frozenset({"anonymous"})
SCHEMES = ("basic", "bearer", "custom")   # custom: the token is the whole value of the header the identity names


@dataclass(frozen=True)
class Identity:
	id: int
	name: str
	upstream_ids: tuple[str, ...]       # bound upstreams, sorted; a token is sent to these only
	scheme: str                     # 'basic' | 'bearer' | 'custom'
	username: str | None            # basic only; not a secret
	header: str | None              # custom only: the header the value is sent under; not a secret
	issued_at: datetime
	expires_at: datetime
	rotate_url: str | None
	last_status: str | None         # 'ok' | 'rejected' | None
	last_status_at: datetime | None

	@property
	def cache_identity(self) -> str:
		"""Cache partition. The id is never reused, so a re-created name starts empty."""
		return f"{self.name}#{self.id}"


@dataclass(frozen=True)
class Approval:
	client_addr: str
	identity: str
	granted_at: datetime


def _iso(value: datetime) -> str:
	if value.tzinfo is None:
		raise ValueError("timestamps must be timezone-aware")
	return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
	return datetime.fromisoformat(value) if value else None


def _row_to_identity(row: sqlite3.Row, upstream_ids: tuple[str, ...]) -> Identity:
	return Identity(
		id=row["id"],
		name=row["name"],
		upstream_ids=upstream_ids,
		scheme=row["scheme"],
		username=row["username"],
		header=row["header"],
		issued_at=_parse(row["issued_at"]),
		expires_at=_parse(row["expires_at"]),
		rotate_url=row["rotate_url"],
		last_status=row["last_status"],
		last_status_at=_parse(row["last_status_at"]),
	)


def check_rotation(*, issued_at: datetime, expires_at: datetime, rotate_url: str | None) -> None:
	"""Raise ValueError for a bad token lifetime or rotate URL."""
	if issued_at.tzinfo is None or expires_at.tzinfo is None:
		raise ValueError("issued_at and expires_at must be timezone-aware")
	if expires_at <= issued_at:
		raise ValueError("expires_at must be after issued_at")
	if rotate_url is not None and (
		not rotate_url.startswith(("http://", "https://"))
		or any(c in rotate_url for c in '"\\ ')
		or _CONTROL_RE.search(rotate_url)
	):
		raise ValueError(f"rotate_url must be a plain http(s) URL: {rotate_url!r}")


def check_identity(
	conn: sqlite3.Connection,
	*,
	name: str,
	scheme: str,
	username: str | None,
	issued_at: datetime,
	expires_at: datetime,
	rotate_url: str | None,
	header: str | None = None,
) -> None:
	"""Raise ValueError for the first problem. The CLI calls this before touching the Keychain."""
	if not _NAME_RE.fullmatch(name):
		raise ValueError(
			f"invalid identity name {name!r}; use up to 64 letters, digits, '.', '_' or '-', "
			"starting with a letter or digit"
		)
	if name.lower() in RESERVED_IDENTITY_NAMES:
		raise ValueError(f"identity name {name!r} is reserved")
	if conn.execute("SELECT 1 FROM identities WHERE name = ?", (name,)).fetchone():   # column is NOCASE
		raise ValueError(f"identity {name!r} already exists")
	if scheme not in SCHEMES:
		raise ValueError(f"scheme must be one of {SCHEMES}: {scheme!r}")
	if scheme == "basic":
		if not username or ":" in username or _CONTROL_RE.search(username):
			raise ValueError("a basic identity needs a username without ':' or control characters")
	elif username is not None:
		raise ValueError(f"a {scheme} identity takes no username")
	if scheme == "custom":
		if not header or not _HEADER_NAME_RE.fullmatch(header):
			raise ValueError("a custom identity needs a header name: letters, digits and the token characters, up to 64")
		if header.lower() in RESERVED_REQUEST_HEADERS:
			raise ValueError(f"a custom identity may not use the header name {header!r}; the proxy sets it itself")
	elif header is not None:
		raise ValueError(f"a {scheme} identity takes no header name")
	check_rotation(issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url)


def _bump_identities_version(conn: sqlite3.Connection) -> None:
	conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'identities_version'")


def create_identity(
	conn: sqlite3.Connection,
	*,
	name: str,
	scheme: str,
	username: str | None = None,
	header: str | None = None,
	issued_at: datetime,
	expires_at: datetime,
	rotate_url: str | None = None,
) -> Identity:
	"""A new identity, bound to no upstream yet (see set_upstream_identities)."""
	check_identity(
		conn, name=name, scheme=scheme, username=username, header=header,
		issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url,
	)
	now = _now()
	with _transaction(conn):
		conn.execute(
			"INSERT INTO identities (name, scheme, username, header, issued_at, expires_at, rotate_url, "
			"created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
			(name, scheme, username, header, _iso(issued_at), _iso(expires_at), rotate_url, now, now),
		)
		_bump_identities_version(conn)
	log.debug("identity created; name=%s scheme=%s header=%s", name, scheme, header)
	return get_identity(conn, name)


def rotate_identity(
	conn: sqlite3.Connection,
	name: str,
	*,
	issued_at: datetime,
	expires_at: datetime,
	rotate_url: str | None = None,
) -> Identity:
	"""Record a new token lifetime. The row (and so the id and cache partition) is kept."""
	check_rotation(issued_at=issued_at, expires_at=expires_at, rotate_url=rotate_url)
	with _transaction(conn):
		cur = conn.execute(
			"UPDATE identities SET issued_at = ?, expires_at = ?, rotate_url = COALESCE(?, rotate_url), "
			"last_status = NULL, last_status_at = NULL, updated_at = ? WHERE name = ?",
			(_iso(issued_at), _iso(expires_at), rotate_url, _now(), name),
		)
		if cur.rowcount == 0:
			raise LookupError(f"no identity named {name!r}")
		_bump_identities_version(conn)
	log.debug("identity rotated; name=%s", name)
	return get_identity(conn, name)


def _bindings(conn: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
	result: dict[int, list[str]] = {}
	for r in conn.execute("SELECT identity_id, upstream_id FROM identity_upstreams ORDER BY upstream_id"):
		result.setdefault(r["identity_id"], []).append(r["upstream_id"])
	return {identity_id: tuple(upstream_ids) for identity_id, upstream_ids in result.items()}


def get_identity(conn: sqlite3.Connection, name: str) -> Identity | None:
	row = conn.execute("SELECT * FROM identities WHERE name = ?", (name,)).fetchone()
	if row is None:
		return None
	upstream_ids = tuple(r["upstream_id"] for r in conn.execute(
		"SELECT upstream_id FROM identity_upstreams WHERE identity_id = ? ORDER BY upstream_id", (row["id"],),
	))
	return _row_to_identity(row, upstream_ids)


def list_identities(conn: sqlite3.Connection) -> list[Identity]:
	bindings = _bindings(conn)
	return [_row_to_identity(r, bindings.get(r["id"], ())) for r in conn.execute("SELECT * FROM identities ORDER BY name")]


def delete_identity(conn: sqlite3.Connection, name: str) -> bool:
	"""Delete an identity; its approvals and bindings go with it (ON DELETE CASCADE)."""
	with _transaction(conn):
		cur = conn.execute("DELETE FROM identities WHERE name = ?", (name,))
		if cur.rowcount:
			_bump_identities_version(conn)
	if cur.rowcount:
		log.debug("identity deleted; name=%s", name)
	return bool(cur.rowcount)


def set_identity_status(conn: sqlite3.Connection, identity_id: int, status: str, at: datetime) -> None:
	conn.execute(
		"UPDATE identities SET last_status = ?, last_status_at = ? WHERE id = ?",
		(status, _iso(at), identity_id),
	)


# --- approvals (Always only; shorter approvals live in the server process) ---

def put_approval(conn: sqlite3.Connection, client_addr: str, identity_id: int) -> None:
	conn.execute(
		"INSERT OR IGNORE INTO approvals (client_addr, identity_id, granted_at) VALUES (?, ?, ?)",
		(client_addr, identity_id, _now()),
	)


def has_approval(conn: sqlite3.Connection, client_addr: str, identity_id: int) -> bool:
	return conn.execute(
		"SELECT 1 FROM approvals WHERE client_addr = ? AND identity_id = ?", (client_addr, identity_id),
	).fetchone() is not None


def list_approvals(conn: sqlite3.Connection) -> list[Approval]:
	rows = conn.execute(
		"SELECT a.client_addr, i.name, a.granted_at FROM approvals a "
		"JOIN identities i ON i.id = a.identity_id ORDER BY i.name, a.client_addr"
	)
	return [Approval(r["client_addr"], r["name"], _parse(r["granted_at"])) for r in rows]


def delete_approval(conn: sqlite3.Connection, client_addr: str, identity_name: str) -> bool:
	cur = conn.execute(
		"DELETE FROM approvals WHERE client_addr = ? "
		"AND identity_id = (SELECT id FROM identities WHERE name = ?)",
		(client_addr, identity_name),
	)
	return bool(cur.rowcount)


# --- rules -------------------------------------------------------------------

def _row_to_rule(row: sqlite3.Row) -> Rule:
	return Rule(
		id=row["id"],
		upstream_id=row["upstream_id"],
		pattern=row["pattern"],
		ttl_seconds=row["ttl_seconds"],
		query_mode=row["query_mode"],
		enabled=bool(row["enabled"]),
		note=row["note"],
		created_at=row["created_at"],
		updated_at=row["updated_at"],
	)


def _check_rule(conn: sqlite3.Connection, upstream_id: str, pattern: str, ttl_seconds: int | None, query_mode: str) -> None:
	if get_upstream(conn, upstream_id) is None:
		raise ValueError(f"no upstream {upstream_id!r}")
	compile_pattern(pattern)
	if ttl_seconds is not None and ttl_seconds < 0:
		raise ValueError(f"ttl_seconds must be None (never), 0 (bypass) or positive: {ttl_seconds}")
	if query_mode not in QUERY_MODES:
		raise ValueError(f"query_mode must be one of {QUERY_MODES}: {query_mode!r}")


def _bump_rules_version(conn: sqlite3.Connection) -> None:
	conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'rules_version'")


def create_rule(
	conn: sqlite3.Connection,
	upstream_id: str,
	pattern: str,
	ttl_seconds: int | None,
	query_mode: str = "include",
	enabled: bool = True,
	note: str | None = None,
) -> Rule:
	"""Create a cache rule for one upstream and bump rules_version atomically."""
	_check_rule(conn, upstream_id, pattern, ttl_seconds, query_mode)
	now = _now()
	with _transaction(conn):
		cur = conn.execute(
			"INSERT INTO rules (upstream_id, pattern, ttl_seconds, query_mode, enabled, note, created_at, updated_at) "
			"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
			(upstream_id, pattern, ttl_seconds, query_mode, int(enabled), note, now, now),
		)
		_bump_rules_version(conn)
	log.debug("rule created; id=%d upstream=%s pattern=%s ttl=%s", cur.lastrowid, upstream_id, pattern, ttl_seconds)
	return _row_to_rule(conn.execute("SELECT * FROM rules WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Rule | None:
	row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
	return _row_to_rule(row) if row else None


def update_rule(
	conn: sqlite3.Connection,
	rule_id: int,
	*,
	upstream_id: str,
	pattern: str,
	ttl_seconds: int | None,
	query_mode: str,
	enabled: bool,
	note: str | None,
) -> Rule:
	"""Replace a rule's fields (its upstream included) and bump rules_version atomically."""
	_check_rule(conn, upstream_id, pattern, ttl_seconds, query_mode)
	with _transaction(conn):
		cur = conn.execute(
			"UPDATE rules SET upstream_id = ?, pattern = ?, ttl_seconds = ?, query_mode = ?, enabled = ?, note = ?, "
			"updated_at = ? WHERE id = ?",
			(upstream_id, pattern, ttl_seconds, query_mode, int(enabled), note, _now(), rule_id),
		)
		if cur.rowcount == 0:
			raise LookupError(f"no rule with id {rule_id}")
		_bump_rules_version(conn)
	log.debug("rule updated; id=%d upstream=%s pattern=%s", rule_id, upstream_id, pattern)
	return get_rule(conn, rule_id)


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
	with _transaction(conn):
		cur = conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
		if cur.rowcount:
			_bump_rules_version(conn)
	if cur.rowcount:
		log.debug("rule deleted; id=%d", rule_id)
	return bool(cur.rowcount)


def list_rules(conn: sqlite3.Connection) -> list[Rule]:
	return [_row_to_rule(r) for r in conn.execute("SELECT * FROM rules ORDER BY upstream_id, id")]


def copy_rules(conn: sqlite3.Connection, source: str, target: str) -> tuple[list[str], list[str]]:
	"""Duplicate the source upstream's rules onto the target, patterns unchanged. Returns (copied, skipped) patterns.

	One transaction, one rules_version bump (when anything copied). Rules are
	copied, not linked: later edits to a source rule don't follow.
	"""
	if source == target:
		raise ValueError("source and target are the same upstream")
	for upstream_id in (source, target):
		if get_upstream(conn, upstream_id) is None:
			raise ValueError(f"no upstream {upstream_id!r}")
	rules = list_rules(conn)
	existing = {r.pattern for r in rules if r.upstream_id == target}
	copied: list[str] = []
	skipped: list[str] = []
	now = _now()
	with _transaction(conn):
		for rule in rules:
			if rule.upstream_id != source:
				continue
			if rule.pattern in existing:
				skipped.append(rule.pattern)
				continue
			conn.execute(
				"INSERT INTO rules (upstream_id, pattern, ttl_seconds, query_mode, enabled, note, created_at, updated_at) "
				"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
				(target, rule.pattern, rule.ttl_seconds, rule.query_mode, int(rule.enabled), rule.note, now, now),
			)
			copied.append(rule.pattern)
		if copied:
			_bump_rules_version(conn)
	log.info("rules copied; source=%s target=%s copied=%d skipped=%d", source, target, len(copied), len(skipped))
	return copied, skipped


def rules_version(conn: sqlite3.Connection) -> int:
	return conn.execute("SELECT value FROM meta WHERE key = 'rules_version'").fetchone()[0]


class RuleCache:
	"""Serves the compiled RuleSet, recompiling only when rules_version changes.

	The version check is one cheap SQLite read per request, and it also notices
	rules written by other processes or connections (e.g. an admin script).
	"""

	def __init__(self, conn: sqlite3.Connection):
		self._conn = conn
		self._version = -1
		self._ruleset = RuleSet([])

	def get(self) -> RuleSet:
		version = rules_version(self._conn)
		if version != self._version:
			self._ruleset = RuleSet(list_rules(self._conn))
			self._version = version
			log.debug("ruleset reloaded; version=%d rules=%d", version, len(self._ruleset))
		return self._ruleset
