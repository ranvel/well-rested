# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from proxy import db

# Plan 1's schema, verbatim: what an existing proxy.db looks like before this plan.
PLAN1_SCHEMA = """
CREATE TABLE upstreams (
	prefix      TEXT PRIMARY KEY,
	base_url    TEXT NOT NULL,
	auth_header TEXT,
	verify_tls  INTEGER NOT NULL DEFAULT 1,
	timeout_s   REAL NOT NULL DEFAULT 30.0
);
CREATE TABLE rules (
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
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO meta (key, value) VALUES ('rules_version', 3);
INSERT INTO upstreams (prefix, base_url, auth_header) VALUES ('jira', 'https://jira.test', 'Authorization');
INSERT INTO rules (pattern, ttl_seconds, created_at, updated_at) VALUES ('/jira/**', 3600, 'x', 'x');
"""


# Plan 1.5's schema (v2), verbatim, with a rule that used the shared flag.
PLAN15_SCHEMA = """
CREATE TABLE upstreams (
	prefix      TEXT PRIMARY KEY,
	base_url    TEXT NOT NULL,
	verify_tls  INTEGER NOT NULL DEFAULT 1,
	timeout_s   REAL NOT NULL DEFAULT 30.0
);
CREATE TABLE rules (
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
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE identities (
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
);
CREATE TABLE approvals (
	client_addr  TEXT NOT NULL,
	identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
	granted_at   TEXT NOT NULL,
	PRIMARY KEY (client_addr, identity_id)
);
INSERT INTO meta (key, value) VALUES ('rules_version', 2), ('identities_version', 1);
INSERT INTO upstreams (prefix, base_url) VALUES ('jira', 'https://jira.test');
INSERT INTO rules (pattern, ttl_seconds, shared, note, created_at, updated_at)
	VALUES ('/jira/rest/api/2/serverInfo', 3600, 1, 'was shared', 'x', 'x');
INSERT INTO identities (name, prefix, scheme, issued_at, expires_at, created_at, updated_at)
	VALUES ('jira-uat', 'jira', 'bearer', '2026-10-01T00:00:00+00:00', '2026-10-31T00:00:00+00:00', 'c', 'd');
PRAGMA user_version = 2;
"""


# Schema v3 (before identities spanned upstreams), verbatim. Identity 3 was deleted, so the
# AUTOINCREMENT high-water mark (3) is above every surviving id.
V3_SCHEMA = """
CREATE TABLE upstreams (
	prefix      TEXT PRIMARY KEY,
	base_url    TEXT NOT NULL,
	verify_tls  INTEGER NOT NULL DEFAULT 1,
	timeout_s   REAL NOT NULL DEFAULT 30.0
);
CREATE TABLE rules (
	id          INTEGER PRIMARY KEY AUTOINCREMENT,
	pattern     TEXT NOT NULL UNIQUE,
	ttl_seconds INTEGER,
	query_mode  TEXT NOT NULL DEFAULT 'include'
	            CHECK (query_mode IN ('include', 'ignore', 'reject')),
	enabled     INTEGER NOT NULL DEFAULT 1,
	note        TEXT,
	created_at  TEXT NOT NULL,
	updated_at  TEXT NOT NULL
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE identities (
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
);
CREATE TABLE approvals (
	client_addr  TEXT NOT NULL,
	identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
	granted_at   TEXT NOT NULL,
	PRIMARY KEY (client_addr, identity_id)
);
INSERT INTO meta (key, value) VALUES ('rules_version', 0), ('identities_version', 7);
INSERT INTO upstreams (prefix, base_url) VALUES ('jira', 'https://jira.test'), ('wiki', 'https://wiki.test');
INSERT INTO identities (name, prefix, scheme, username, issued_at, expires_at, rotate_url, last_status, created_at, updated_at)
	VALUES ('jira-uat', 'jira', 'basic', 'ran@example.com', '2026-10-01T00:00:00+00:00', '2026-10-31T00:00:00+00:00',
		'https://id.test/rotate', 'ok', 'c', 'd');
INSERT INTO identities (name, prefix, scheme, issued_at, expires_at, created_at, updated_at)
	VALUES ('wiki-bot', 'wiki', 'bearer', '2026-10-01T00:00:00+00:00', '2026-10-31T00:00:00+00:00', 'c', 'd');
INSERT INTO identities (name, prefix, scheme, issued_at, expires_at, created_at, updated_at)
	VALUES ('gone', 'wiki', 'bearer', '2026-10-01T00:00:00+00:00', '2026-10-31T00:00:00+00:00', 'c', 'd');
DELETE FROM identities WHERE name = 'gone';
INSERT INTO approvals (client_addr, identity_id, granted_at) VALUES ('192.0.2.111', 2, '2026-10-02T00:00:00+00:00');
PRAGMA user_version = 3;
"""


def columns(conn, table):
	return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_fresh_database_is_created_at_the_current_version(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 9
	assert "auth_header" not in columns(conn, "upstreams")
	assert columns(conn, "upstreams")[0] == "upstream_id"
	assert "prefix" not in columns(conn, "identities")
	assert columns(conn, "identity_upstreams") == ["identity_id", "upstream_id"]
	assert "shared" not in columns(conn, "rules")
	assert columns(conn, "rules")[:3] == ["id", "upstream_id", "pattern"]
	assert "name" in columns(conn, "identities") and "header" in columns(conn, "identities")
	assert "identity_id" in columns(conn, "approvals")
	assert db.rules_version(conn) == 0
	assert db.identities_version(conn) == 0


def test_plan1_database_is_migrated_in_place(tmp_path):
	path = tmp_path / "p.db"
	legacy = sqlite3.connect(path)
	legacy.executescript(PLAN1_SCHEMA)
	legacy.close()

	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert "auth_header" not in columns(conn, "upstreams")
	assert "shared" not in columns(conn, "rules")
	assert db.get_upstream(conn, "jira") == db.Upstream("jira", "https://jira.test")
	assert [(r.upstream_id, r.pattern) for r in db.list_rules(conn)] == [("jira", "/**")]
	assert db.rules_version(conn) == 3


def test_plan15_database_loses_shared_but_keeps_its_rules(tmp_path):
	path = tmp_path / "p.db"
	legacy = sqlite3.connect(path)
	legacy.executescript(PLAN15_SCHEMA)
	legacy.close()

	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert "shared" not in columns(conn, "rules")
	[rule] = db.list_rules(conn)
	assert (rule.upstream_id, rule.pattern, rule.ttl_seconds, rule.note) == ("jira", "/rest/api/2/serverInfo", 3600, "was shared")
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("jira",)
	assert db.rules_version(conn) == 2
	assert db.identities_version(conn) == 1


def test_connecting_again_is_a_no_op(tmp_path):
	db.connect(tmp_path / "p.db").close()
	conn = db.connect(tmp_path / "p.db")
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v3_identities_keep_ids_fields_and_approvals_and_gain_a_binding(tmp_path):
	path = tmp_path / "p.db"
	legacy = sqlite3.connect(path)
	legacy.executescript(V3_SCHEMA)
	legacy.close()

	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert "prefix" not in columns(conn, "identities")
	uat, bot = db.list_identities(conn)
	assert (uat.id, uat.name, uat.upstream_ids, uat.username, uat.rotate_url, uat.last_status) == (
		1, "jira-uat", ("jira",), "ran@example.com", "https://id.test/rotate", "ok",
	)
	assert (bot.id, bot.name, bot.upstream_ids, bot.scheme) == (2, "wiki-bot", ("wiki",), "bearer")
	assert [(a.client_addr, a.identity) for a in db.list_approvals(conn)] == [("192.0.2.111", "wiki-bot")]
	assert db.identities_version(conn) == 7
	assert db.get_identity(conn, "JIRA-UAT").name == "jira-uat"   # name keeps COLLATE NOCASE


def test_v3_migration_keeps_the_autoincrement_high_water_mark(tmp_path):
	path = tmp_path / "p.db"
	legacy = sqlite3.connect(path)
	legacy.executescript(V3_SCHEMA)
	legacy.close()

	conn = db.connect(path)
	now = datetime.now(timezone.utc)
	created = db.create_identity(conn, name="new", scheme="bearer", issued_at=now, expires_at=now + timedelta(days=9))
	assert created.id == 4   # 3 belonged to the deleted 'gone'; ids are never reused


def test_v3_migration_keeps_cascades_working(tmp_path):
	path = tmp_path / "p.db"
	legacy = sqlite3.connect(path)
	legacy.executescript(V3_SCHEMA)
	legacy.close()

	conn = db.connect(path)
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
	db.delete_identity(conn, "wiki-bot")
	assert db.list_approvals(conn) == []
	assert conn.execute("SELECT COUNT(*) FROM identity_upstreams").fetchone()[0] == 1


def test_a_newer_schema_is_refused(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	conn.execute("PRAGMA user_version = 99")
	conn.close()
	with pytest.raises(RuntimeError, match="v99"):
		db.connect(tmp_path / "p.db")


def test_foreign_keys_are_enforced(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_put_upstream_updates_a_referenced_upstream(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	conn.execute(
		"INSERT INTO identities (name, scheme, issued_at, expires_at, created_at, updated_at) "
		"VALUES ('jira-uat', 'bearer', 'a', 'b', 'c', 'd')"
	)
	conn.execute("INSERT INTO identity_upstreams (identity_id, upstream_id) VALUES (1, 'jira')")
	updated = db.put_upstream(conn, db.Upstream("jira", "https://jira2.test", timeout_s=5.0))
	assert updated == db.Upstream("jira", "https://jira2.test", timeout_s=5.0)


def make_v4(path):
	"""A v4 database built from the shipped migrations, with one upstream."""
	legacy = sqlite3.connect(path)
	for statements in db._MIGRATIONS[:4]:
		for statement in statements:
			legacy.execute(statement)
	legacy.execute("PRAGMA user_version = 4")
	legacy.execute("INSERT INTO upstreams (prefix, base_url, timeout_s) VALUES ('jira', 'https://jira.test', 12.5)")
	legacy.commit()
	legacy.close()


def test_v4_gains_proxies_and_upstreams_stay_direct(tmp_path):
	path = tmp_path / "p.db"
	make_v4(path)
	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert "proxy" in columns(conn, "upstreams")
	assert db.get_upstream(conn, "jira") == db.Upstream("jira", "https://jira.test", timeout_s=12.5)
	assert db.list_proxies(conn) == []
	assert db.proxies_version(conn) == 0
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def make_v5(path):
	"""A v5 database built from the shipped migrations: two identities (a third was deleted), bindings, an approval."""
	legacy = sqlite3.connect(path)
	for statements in db._MIGRATIONS[:5]:
		for statement in statements:
			legacy.execute(statement)
	legacy.execute("PRAGMA user_version = 5")
	legacy.executescript("""
		INSERT INTO upstreams (prefix, base_url) VALUES ('jira', 'https://jira.test'), ('wiki', 'https://wiki.test');
		INSERT INTO identities (name, scheme, username, issued_at, expires_at, rotate_url, last_status, created_at, updated_at)
			VALUES ('jira-uat', 'basic', 'ran@example.com', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00',
				'https://id.test/rotate', 'ok', 'c', 'u');
		INSERT INTO identities (name, scheme, issued_at, expires_at, created_at, updated_at)
			VALUES ('gone', 'bearer', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'c', 'u');
		INSERT INTO identities (name, scheme, issued_at, expires_at, created_at, updated_at)
			VALUES ('wiki-bot', 'bearer', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'c', 'u');
		DELETE FROM identities WHERE name = 'gone';
		INSERT INTO identity_upstreams (identity_id, prefix) VALUES (1, 'jira'), (1, 'wiki'), (3, 'wiki');
		INSERT INTO approvals (client_addr, identity_id, granted_at) VALUES ('192.0.2.111', 3, '2026-10-02T00:00:00+00:00');
		UPDATE meta SET value = 4 WHERE key = 'identities_version';
	""")
	legacy.commit()
	legacy.close()


def test_v5_identities_keep_ids_bindings_and_approvals(tmp_path):
	path = tmp_path / "p.db"
	make_v5(path)
	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	uat, bot = db.list_identities(conn)
	assert (uat.id, uat.name, uat.upstream_ids, uat.username, uat.rotate_url, uat.last_status) == (
		1, "jira-uat", ("jira", "wiki"), "ran@example.com", "https://id.test/rotate", "ok",
	)
	assert (bot.id, bot.upstream_ids, bot.scheme) == (3, ("wiki",), "bearer")
	assert [(a.client_addr, a.identity) for a in db.list_approvals(conn)] == [("192.0.2.111", "wiki-bot")]
	assert db.identities_version(conn) == 4
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
	assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"identities", "identity_upstreams", "approvals"}
	assert not [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_%' ESCAPE '\\'")]


def test_v5_migration_allows_custom_and_keeps_the_high_water_mark_and_cascades(tmp_path):
	path = tmp_path / "p.db"
	make_v5(path)
	conn = db.connect(path)
	now = datetime.now(timezone.utc)
	created = db.create_identity(conn, name="proj", scheme="custom", header="Authorization", issued_at=now, expires_at=now + timedelta(days=9))
	assert (created.id, created.scheme) == (4, "custom")   # 2 belonged to the deleted 'gone'; ids are never reused
	db.delete_identity(conn, "wiki-bot")
	assert db.list_approvals(conn) == []
	assert conn.execute("SELECT COUNT(*) FROM identity_upstreams").fetchone()[0] == 2


def test_v6_upstreams_and_bindings_are_keyed_by_upstream_id(tmp_path):
	path = tmp_path / "p.db"
	make_v5(path)
	legacy = sqlite3.connect(path)
	for statement in db._MIGRATIONS[5]:
		legacy.execute(statement)
	legacy.execute("PRAGMA user_version = 6")
	legacy.commit()
	legacy.close()
	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert [u.upstream_id for u in db.list_upstreams(conn)] == ["jira", "wiki"]
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("jira", "wiki")
	assert [(a.client_addr, a.identity) for a in db.list_approvals(conn)] == [("192.0.2.111", "wiki-bot")]
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
	db.delete_upstream(conn, "wiki")   # the renamed column still cascades to the bindings
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("jira",)
	assert db.get_identity(conn, "wiki-bot").upstream_ids == ()


def make_v7(path):
	"""A v7 database built from the shipped migrations, with path-prefixed rules (one deleted, so the
	AUTOINCREMENT high-water mark sits above the highest id)."""
	legacy = sqlite3.connect(path)
	for statements in db._MIGRATIONS[:7]:
		for statement in statements:
			legacy.execute(statement)
	legacy.execute("PRAGMA user_version = 7")
	legacy.execute(
		"INSERT INTO upstreams (upstream_id, base_url) VALUES "
		"('jira', 'https://jira.test'), ('wiki', 'https://wiki.test'), ('a_b', 'https://ab.test')"
	)
	patterns = [
		"/jira/rest/**",   # 1 → (jira, /rest/**)
		"/jira",           # 2 → (jira, /)
		"/jira/",          # 3 → (jira, /) too: dropped, rule 2 is older
		"/wiki/**",        # 4 → (wiki, /**)
		"/**",             # 5 no literal upstream segment: dropped
		"/jirax/**",       # 6 not under /jira/: dropped
		"/JIRA/x",         # 7 routing was case-sensitive: dropped
		"/aXb/x",          # 8 "_" is no wildcard: dropped
		"/a_b/x",          # 9 → (a_b, /x)
		"/ghost/x",        # 10 no such upstream: dropped
		"/jira/gone",      # 11 deleted below
	]
	for pattern in patterns:
		legacy.execute(
			"INSERT INTO rules (pattern, ttl_seconds, query_mode, enabled, note, created_at, updated_at) "
			"VALUES (?, 60, 'ignore', 0, 'n', 'c', 'u')", (pattern,),
		)
	legacy.execute("DELETE FROM rules WHERE pattern = '/jira/gone'")
	legacy.commit()
	legacy.close()


def test_v7_rules_join_the_upstream_their_pattern_began_with(tmp_path):
	path = tmp_path / "p.db"
	make_v7(path)
	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	assert [(r.id, r.upstream_id, r.pattern) for r in db.list_rules(conn)] == [
		(9, "a_b", "/x"), (1, "jira", "/rest/**"), (2, "jira", "/"), (4, "wiki", "/**"),
	]
	rule = db.get_rule(conn, 1)
	assert (rule.ttl_seconds, rule.query_mode, rule.enabled, rule.note, rule.created_at) == (60, "ignore", False, "n", "c")
	assert db.create_rule(conn, "jira", "/new", 60).id == 12   # 11 was used and deleted; ids are never reused
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
	assert not [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_%' ESCAPE '\\'")]


def test_v8_rules_are_unique_per_upstream_and_go_with_it(tmp_path):
	path = tmp_path / "p.db"
	make_v7(path)
	conn = db.connect(path)
	db.create_rule(conn, "wiki", "/rest/**", 60)   # jira has /rest/** too
	with pytest.raises(sqlite3.IntegrityError):
		db.create_rule(conn, "jira", "/rest/**", 60)
	db.delete_upstream(conn, "jira")
	assert [(r.upstream_id, r.pattern) for r in db.list_rules(conn)] == [("a_b", "/x"), ("wiki", "/**"), ("wiki", "/rest/**")]


def make_v8(path):
	"""A v8 database built from the shipped migrations, with one identity per scheme (one deleted)."""
	legacy = sqlite3.connect(path)
	for statements in db._MIGRATIONS[:8]:
		for statement in statements:
			legacy.execute(statement)
	legacy.execute("PRAGMA user_version = 8")
	legacy.executescript("""
		INSERT INTO upstreams (upstream_id, base_url) VALUES ('jira', 'https://jira.test'), ('api', 'https://api.test');
		INSERT INTO identities (name, scheme, username, issued_at, expires_at, created_at, updated_at)
			VALUES ('jira-uat', 'basic', 'ran@example.com', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'c', 'u');
		INSERT INTO identities (name, scheme, issued_at, expires_at, created_at, updated_at)
			VALUES ('gone', 'bearer', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'c', 'u');
		INSERT INTO identities (name, scheme, issued_at, expires_at, last_status, created_at, updated_at)
			VALUES ('proj', 'custom', '2026-10-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', 'ok', 'c', 'u');
		DELETE FROM identities WHERE name = 'gone';
		INSERT INTO identity_upstreams (identity_id, upstream_id) VALUES (1, 'jira'), (3, 'api');
		INSERT INTO approvals (client_addr, identity_id, granted_at) VALUES ('192.0.2.111', 3, '2026-10-02T00:00:00+00:00');
		UPDATE meta SET value = 7 WHERE key = 'identities_version';
	""")
	legacy.commit()
	legacy.close()


def test_v8_custom_identities_gain_the_authorization_header_name(tmp_path):
	path = tmp_path / "p.db"
	make_v8(path)
	conn = db.connect(path)
	assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
	uat, proj = db.list_identities(conn)
	assert (uat.id, uat.scheme, uat.username, uat.header, uat.upstream_ids) == (1, "basic", "ran@example.com", None, ("jira",))
	assert (proj.id, proj.scheme, proj.username, proj.header, proj.upstream_ids, proj.last_status) == (
		3, "custom", None, "Authorization", ("api",), "ok",
	)
	assert [(a.client_addr, a.identity) for a in db.list_approvals(conn)] == [("192.0.2.111", "proj")]
	assert db.identities_version(conn) == 7
	assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
	now = datetime.now(timezone.utc)
	created = db.create_identity(conn, name="claude", scheme="custom", header="x-api-key", issued_at=now, expires_at=now + timedelta(days=9))
	assert (created.id, created.header) == (4, "x-api-key")   # 2 belonged to the deleted 'gone'; ids are never reused
