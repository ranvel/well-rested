# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import sqlite3

import pytest

from proxy import db
from proxy.db import Upstream


@pytest.fixture
def conn(tmp_path):
	c = db.connect(tmp_path / "proxy.db")
	yield c
	c.close()


def test_connect_uses_wal(conn):
	assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connect_twice_keeps_data(tmp_path):
	first = db.connect(tmp_path / "p.db")
	db.put_upstream(first, Upstream("jira", "https://jira.test"))
	db.create_rule(first, "jira", "/**", 60)
	first.close()
	second = db.connect(tmp_path / "p.db")
	assert [r.pattern for r in db.list_rules(second)] == ["/**"]
	assert db.rules_version(second) == 1
	second.close()


def test_put_and_get_upstream_strips_trailing_slash(conn):
	db.put_upstream(conn, Upstream("jira", "https://jira.test/", timeout_s=5.0))
	assert db.get_upstream(conn, "jira") == Upstream("jira", "https://jira.test", verify_tls=True, timeout_s=5.0)


def test_put_upstream_replaces_existing(conn):
	db.put_upstream(conn, Upstream("jira", "https://old.test"))
	db.put_upstream(conn, Upstream("jira", "https://new.test", verify_tls=False))
	up = db.get_upstream(conn, "jira")
	assert up.base_url == "https://new.test"
	assert up.verify_tls is False


def test_get_unknown_upstream_is_none(conn):
	assert db.get_upstream(conn, "nope") is None


@pytest.mark.parametrize("upstream_id", ["", "_admin", "a/b", "..", "-x", "a b"])
def test_put_upstream_rejects_bad_prefix(conn, upstream_id):
	with pytest.raises(ValueError, match="invalid upstream id"):
		db.put_upstream(conn, Upstream(upstream_id, "https://x.test"))


def test_favicon_is_an_ordinary_upstream_id_now_that_ids_are_not_paths(conn):
	assert db.put_upstream(conn, Upstream("favicon.ico", "https://x.test")).upstream_id == "favicon.ico"


def test_put_upstream_rejects_non_http_url(conn):
	with pytest.raises(ValueError, match="base_url must be http"):
		db.put_upstream(conn, Upstream("jira", "ftp://jira.test"))


def test_put_upstream_refuses_a_case_collision(conn):
	db.put_upstream(conn, Upstream("jira", "https://jira.test"))
	with pytest.raises(ValueError, match="collides with existing 'jira'"):
		db.put_upstream(conn, Upstream("Jira", "https://other.test"))
	db.put_upstream(conn, Upstream("jira", "https://jira2.test"))   # same spelling: an edit
	assert [u.upstream_id for u in db.list_upstreams(conn)] == ["jira"]


def test_delete_upstream(conn):
	db.put_upstream(conn, Upstream("jira", "https://jira.test"))
	assert db.delete_upstream(conn, "jira") is True
	assert db.delete_upstream(conn, "jira") is False
	assert db.list_upstreams(conn) == []


def test_seed_only_on_empty_table(conn):
	assert db.seed_upstreams(conn, [("jira", "https://jira.test"), ("wiki", "https://wiki.test")]) == 2
	assert db.seed_upstreams(conn, [("other", "https://other.test")]) == 0
	assert [u.upstream_id for u in db.list_upstreams(conn)] == ["jira", "wiki"]


def test_seed_skips_duplicate_prefixes(conn):
	assert db.seed_upstreams(conn, [("jira", "https://a.test"), ("jira", "https://b.test")]) == 1
	assert db.get_upstream(conn, "jira").base_url == "https://a.test"


@pytest.fixture
def rconn(conn):
	"""conn with two upstreams for rules to belong to."""
	db.put_upstream(conn, Upstream("jira", "https://jira.test"))
	db.put_upstream(conn, Upstream("wiki", "https://wiki.test"))
	return conn


def test_create_rule_round_trips_and_bumps_version(rconn):
	r = db.create_rule(rconn, "jira", "/**", 3600, query_mode="ignore", note="all jira")
	assert r.id > 0
	assert (r.upstream_id, r.pattern, r.ttl_seconds, r.query_mode, r.enabled, r.note) == (
		"jira", "/**", 3600, "ignore", True, "all jira",
	)
	assert r.created_at and r.updated_at
	assert db.rules_version(rconn) == 1
	assert db.list_rules(rconn) == [r]


def test_create_rule_never_ttl_is_null(rconn):
	assert db.create_rule(rconn, "jira", "/x", None).ttl_seconds is None


@pytest.mark.parametrize("kwargs", [
	dict(upstream_id="jira", pattern="rest/**", ttl_seconds=60),
	dict(upstream_id="jira", pattern="/**", ttl_seconds=-1),
	dict(upstream_id="jira", pattern="/**", ttl_seconds=60, query_mode="sometimes"),
	dict(upstream_id="nope", pattern="/**", ttl_seconds=60),
])
def test_create_rule_rejects_bad_input_without_version_bump(rconn, kwargs):
	with pytest.raises(ValueError):
		db.create_rule(rconn, **kwargs)
	assert db.rules_version(rconn) == 0
	assert db.list_rules(rconn) == []


def test_duplicate_pattern_rolls_back(rconn):
	db.create_rule(rconn, "jira", "/**", 60)
	with pytest.raises(sqlite3.IntegrityError):
		db.create_rule(rconn, "jira", "/**", 120)
	assert db.rules_version(rconn) == 1
	assert len(db.list_rules(rconn)) == 1


def test_the_same_pattern_may_exist_once_per_upstream(rconn):
	db.create_rule(rconn, "jira", "/rest/**", 60)
	db.create_rule(rconn, "wiki", "/rest/**", 120)
	assert [(r.upstream_id, r.ttl_seconds) for r in db.list_rules(rconn)] == [("jira", 60), ("wiki", 120)]


def test_get_rule(rconn):
	created = db.create_rule(rconn, "jira", "/**", 60)
	assert db.get_rule(rconn, created.id) == created
	assert db.get_rule(rconn, 999) is None


def test_update_rule_replaces_fields_and_bumps_version(rconn):
	created = db.create_rule(rconn, "jira", "/**", 60)
	updated = db.update_rule(
		rconn, created.id, upstream_id="wiki", pattern="/rest/**", ttl_seconds=None, query_mode="ignore",
		enabled=False, note="n",
	)
	assert (updated.id, updated.upstream_id, updated.pattern, updated.ttl_seconds, updated.query_mode, updated.enabled, updated.note) == (
		created.id, "wiki", "/rest/**", None, "ignore", False, "n",
	)
	assert updated.created_at == created.created_at
	assert updated.updated_at >= created.updated_at
	assert db.rules_version(rconn) == 2
	assert db.list_rules(rconn) == [updated]


def test_update_unknown_rule_is_lookup_error(rconn):
	with pytest.raises(LookupError):
		db.update_rule(rconn, 42, upstream_id="jira", pattern="/x", ttl_seconds=1, query_mode="include", enabled=True, note=None)
	assert db.rules_version(rconn) == 0


@pytest.mark.parametrize("kwargs", [
	dict(pattern="rest/**"),
	dict(ttl_seconds=-1),
	dict(query_mode="sometimes"),
	dict(upstream_id="nope"),
])
def test_update_rule_rejects_bad_input_without_version_bump(rconn, kwargs):
	created = db.create_rule(rconn, "jira", "/**", 60)
	fields = dict(upstream_id="jira", pattern="/**", ttl_seconds=60, query_mode="include", enabled=True, note=None)
	with pytest.raises(ValueError):
		db.update_rule(rconn, created.id, **{**fields, **kwargs})
	assert db.rules_version(rconn) == 1
	assert db.get_rule(rconn, created.id) == created


def test_update_rule_to_a_taken_pattern_rolls_back(rconn):
	db.create_rule(rconn, "jira", "/**", 60)
	other = db.create_rule(rconn, "wiki", "/**", 60)
	with pytest.raises(sqlite3.IntegrityError):
		db.update_rule(rconn, other.id, upstream_id="jira", pattern="/**", ttl_seconds=60, query_mode="include", enabled=True, note=None)
	assert db.rules_version(rconn) == 2
	assert db.get_rule(rconn, other.id) == other


def test_delete_rule(rconn):
	created = db.create_rule(rconn, "jira", "/**", 60)
	assert db.delete_rule(rconn, created.id) is True
	assert db.rules_version(rconn) == 2
	assert db.delete_rule(rconn, created.id) is False
	assert db.rules_version(rconn) == 2
	assert db.list_rules(rconn) == []


def test_deleting_an_upstream_deletes_its_rules_and_bumps_the_version(rconn):
	db.create_rule(rconn, "jira", "/**", 60)
	kept = db.create_rule(rconn, "wiki", "/**", 60)
	assert db.delete_upstream(rconn, "jira") is True
	assert db.list_rules(rconn) == [kept]
	assert db.rules_version(rconn) == 3


def test_deleting_an_upstream_without_rules_leaves_the_version(rconn):
	assert db.delete_upstream(rconn, "wiki") is True
	assert db.rules_version(rconn) == 0


# --- copy rules from one upstream to another ------------------------------------

@pytest.fixture
def copy_setup(conn):
	db.put_upstream(conn, Upstream("jira", "https://jira.test"))
	db.put_upstream(conn, Upstream("jira-prod", "https://jira-prod.test"))
	db.put_upstream(conn, Upstream("wiki", "https://wiki.test"))
	db.create_rule(conn, "jira", "/rest/**", 3600, query_mode="ignore", note="rest")
	db.create_rule(conn, "jira", "/off/*", 5, enabled=False)
	db.create_rule(conn, "wiki", "/wiki/**", 1)            # another upstream's: never copied
	db.create_rule(conn, "jira-prod", "/rest/**", 60)     # already on the target
	return conn


def test_copy_rules_copies_fields_and_skips_existing(copy_setup):
	conn = copy_setup
	version = db.rules_version(conn)
	copied, skipped = db.copy_rules(conn, "jira", "jira-prod")
	assert copied == ["/off/*"]
	assert skipped == ["/rest/**"]
	by_key = {(r.upstream_id, r.pattern): r for r in db.list_rules(conn)}
	new = by_key[("jira-prod", "/off/*")]
	assert (new.ttl_seconds, new.query_mode, new.enabled, new.note) == (5, "include", False, None)
	assert by_key[("jira-prod", "/rest/**")].ttl_seconds == 60    # the target's own rule is untouched
	assert by_key[("jira", "/rest/**")].note == "rest"            # the source is untouched
	assert ("jira-prod", "/wiki/**") not in by_key and len(by_key) == 5
	assert db.rules_version(conn) == version + 1


def test_copy_rules_keeps_ttl_mode_and_note(copy_setup):
	conn = copy_setup
	db.delete_rule(conn, next(r.id for r in db.list_rules(conn) if (r.upstream_id, r.pattern) == ("jira-prod", "/rest/**")))
	copied, _ = db.copy_rules(conn, "jira", "jira-prod")
	rest = next(r for r in db.list_rules(conn) if (r.upstream_id, r.pattern) == ("jira-prod", "/rest/**"))
	assert (rest.ttl_seconds, rest.query_mode, rest.enabled, rest.note) == (3600, "ignore", True, "rest")
	assert copied == ["/rest/**", "/off/*"]


def test_copy_rules_all_skipped_bumps_nothing(copy_setup):
	conn = copy_setup
	db.copy_rules(conn, "jira", "jira-prod")
	version = db.rules_version(conn)
	copied, skipped = db.copy_rules(conn, "jira", "jira-prod")
	assert copied == [] and skipped == ["/rest/**", "/off/*"]
	assert db.rules_version(conn) == version


def test_copy_rules_touches_only_the_rules_table(copy_setup):
	from datetime import datetime, timedelta, timezone
	conn = copy_setup
	now = datetime.now(timezone.utc)
	identity = db.create_identity(conn, name="uat", scheme="bearer", issued_at=now, expires_at=now + timedelta(days=9))
	db.set_upstream_identities(conn, "jira", ["uat"])
	db.put_approval(conn, "192.0.2.111", identity.id)
	before = (db.list_identities(conn), db.list_approvals(conn), db.identities_version(conn))
	db.copy_rules(conn, "jira", "jira-prod")
	assert (db.list_identities(conn), db.list_approvals(conn), db.identities_version(conn)) == before


@pytest.mark.parametrize("source, target, fragment", [
	("jira", "jira", "same"),
	("jira", "nope", "nope"),
	("nope", "jira", "nope"),
])
def test_copy_rules_refuses(copy_setup, source, target, fragment):
	with pytest.raises(ValueError, match=fragment):
		db.copy_rules(copy_setup, source, target)


def test_rule_cache_sees_rules_written_by_another_connection(tmp_path):
	app_conn = db.connect(tmp_path / "proxy.db")
	admin_conn = db.connect(tmp_path / "proxy.db")
	db.put_upstream(admin_conn, Upstream("jira", "https://jira.test"))
	cache = db.RuleCache(app_conn)
	assert cache.get().match("jira", "/x") is None
	db.create_rule(admin_conn, "jira", "/**", 60)
	assert cache.get().match("jira", "/x").pattern == "/**"
	unchanged = cache.get()
	assert cache.get() is unchanged
	app_conn.close()
	admin_conn.close()


def test_seeding_skips_an_id_that_differs_only_in_case(conn):
	assert db.seed_upstreams(conn, [("jira", "https://a.test"), ("JIRA", "https://b.test")]) == 1
	assert [u.upstream_id for u in db.list_upstreams(conn)] == ["jira"]


def test_resolve_upstream_prefers_the_exact_spelling(conn):
	db.put_upstream(conn, Upstream("jira", "https://a.test"))
	conn.execute("INSERT INTO upstreams (upstream_id, base_url) VALUES ('JIRA', 'https://b.test')")   # as an older seed could
	assert db.resolve_upstream(conn, "JIRA").base_url == "https://b.test"
	assert db.resolve_upstream(conn, "jira").base_url == "https://a.test"
	assert db.resolve_upstream(conn, "Jira") is not None
