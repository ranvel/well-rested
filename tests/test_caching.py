# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import gzip
import time

import httpx
import pytest

from proxy import db


def shift_clock(monkeypatch, seconds):
	real = time.time
	monkeypatch.setattr(time, "time", lambda: real() + seconds)


@pytest.fixture
def authed(admin_db):
	"""Everything under jira cached for 1h."""
	db.create_rule(admin_db, "jira", "/**", 3600)


# --- basic flow -------------------------------------------------------------

def test_miss_then_hit(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	first = client.get("/rest/api/2/issue/ABC-1")
	second = client.get("/rest/api/2/issue/ABC-1")
	assert first.headers["x-cache"] == "MISS"
	assert second.headers["x-cache"] == "HIT"
	assert second.json() == first.json()
	assert int(second.headers["age"]) >= 0
	assert len(fake_upstream.requests) == 1


def test_no_rule_never_caches(client, fake_upstream):
	client.get("/x")
	assert client.get("/x").headers["x-cache"] == "PASS"
	assert len(fake_upstream.requests) == 2


def test_new_rule_takes_effect_without_restart(client, admin_db):
	assert client.get("/x").headers["x-cache"] == "PASS"
	db.create_rule(admin_db, "jira", "/**", 3600)
	assert client.get("/x").headers["x-cache"] == "MISS"
	assert client.get("/x").headers["x-cache"] == "HIT"


def test_bypass_rule_carves_out_of_broader_rule(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/rest/**", 3600)
	db.create_rule(admin_db, "jira", "/rest/api/2/myself", 0)
	assert client.get("/rest/api/2/myself").headers["x-cache"] == "PASS"
	assert client.get("/rest/api/2/myself").headers["x-cache"] == "PASS"
	assert len(fake_upstream.requests) == 2


def test_stale_entry_is_refreshed(client, admin_db, fake_upstream, monkeypatch):
	db.create_rule(admin_db, "jira", "/**", 60)
	client.get("/x")
	shift_clock(monkeypatch, 61)
	assert client.get("/x").headers["x-cache"] == "REFRESH"
	assert client.get("/x").headers["x-cache"] == "HIT"
	assert len(fake_upstream.requests) == 2


def test_never_ttl_does_not_expire(client, admin_db, monkeypatch):
	db.create_rule(admin_db, "jira", "/**", None)
	client.get("/x")
	shift_clock(monkeypatch, 10 * 365 * 86400)
	assert client.get("/x").headers["x-cache"] == "HIT"


def test_only_200_is_cached(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	fake_upstream.responder = lambda req: httpx.Response(404, json={"error": "nope"})
	assert client.get("/x").status_code == 404
	assert client.get("/x").headers["x-cache"] == "MISS"
	assert len(fake_upstream.requests) == 2


def test_oversize_response_served_but_not_cached(make_client, settings, fake_upstream):
	c = make_client(max_response_bytes=10)
	conn = db.connect(settings.db_path)
	db.create_rule(conn, "jira", "/**", 3600)
	conn.close()
	fake_upstream.responder = lambda req: httpx.Response(200, content=b"x" * 11)
	assert c.get("/x").content == b"x" * 11
	assert c.get("/x").headers["x-cache"] == "MISS"


def test_non_get_is_never_cached(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	assert client.post("/x", content=b"{}").headers["x-cache"] == "PASS"
	assert client.post("/x", content=b"{}").headers["x-cache"] == "PASS"
	assert client.get("/x").headers["x-cache"] == "MISS"


def test_upstream_failure_on_miss_is_502(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	def refused(req):
		raise httpx.ConnectError("refused", request=req)
	fake_upstream.responder = refused
	assert client.get("/x").status_code == 502


# --- query modes -------------------------------------------------------------

def test_include_mode_keys_on_sorted_query(client, admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600)
	client.get("/search?a=1&b=2")
	assert client.get("/search?b=2&a=1").headers["x-cache"] == "HIT"
	assert client.get("/search?a=1&b=3").headers["x-cache"] == "MISS"


def test_ignore_mode_shares_one_entry(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600, query_mode="ignore")
	client.get("/search?a=1")
	assert client.get("/search?a=2").headers["x-cache"] == "HIT"
	assert client.get("/search").headers["x-cache"] == "HIT"
	assert len(fake_upstream.requests) == 1


def test_reject_mode_passes_queries_through(client, admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600, query_mode="reject")
	assert client.get("/search?a=1").headers["x-cache"] == "PASS"
	assert client.get("/search").headers["x-cache"] == "MISS"
	assert client.get("/search").headers["x-cache"] == "HIT"


# --- header hygiene on cached entries ----------------------------------------

def test_cached_gzip_response_served_decoded(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	payload = b'{"ok":true}'
	fake_upstream.responder = lambda req: httpx.Response(
		200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
	)
	client.get("/x")
	hit = client.get("/x")
	assert hit.headers["x-cache"] == "HIT"
	assert hit.content == payload
	assert "content-encoding" not in hit.headers
	assert hit.headers["content-length"] == str(len(payload))
	assert hit.headers["content-type"] == "application/json"


def test_set_cookie_never_replayed_from_cache(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	fake_upstream.responder = lambda req: httpx.Response(200, headers={"Set-Cookie": "session=abc"})
	miss = client.get("/x")
	hit = client.get("/x")
	assert "set-cookie" not in miss.headers
	assert hit.headers["x-cache"] == "HIT"
	assert "set-cookie" not in hit.headers


def test_upstream_cdn_headers_do_not_leak_into_our_contract(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	fake_upstream.responder = lambda req: httpx.Response(200, headers={"X-Cache": "Miss from cloudfront", "Age": "30"})
	first = client.get("/x")
	assert first.headers.get_list("x-cache") == ["MISS"]
	assert first.headers.get_list("age") == ["30"]
	second = client.get("/x")
	assert second.headers.get_list("x-cache") == ["HIT"]
	assert len(second.headers.get_list("age")) == 1
	assert int(second.headers["age"]) < 30


def test_rule_ttl_change_applies_to_existing_entries(client, admin_db, fake_upstream, monkeypatch):
	db.create_rule(admin_db, "jira", "/**", None)
	assert client.get("/issue/X").headers["x-cache"] == "MISS"
	db.create_rule(admin_db, "jira", "/issue/*", 60)
	shift_clock(monkeypatch, 61)
	assert client.get("/issue/X").headers["x-cache"] == "REFRESH"


def test_cache_read_failure_is_a_miss(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)

	async def boom(upstream_id, key):
		raise RuntimeError("corrupt shelf")

	client.app.state.cache.get = boom
	r = client.get("/x")
	assert r.status_code == 200
	assert r.headers["x-cache"] == "MISS"


def test_cache_write_failure_still_serves(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)

	async def boom(upstream_id, key, entry):
		raise RuntimeError("disk full")

	client.app.state.cache.put = boom
	r = client.get("/x")
	assert r.status_code == 200
	assert r.json() == {"ok": True}
	assert r.headers["x-cache"] == "MISS"


def test_get_with_body_is_never_cached(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	for _ in range(2):
		r = client.request("GET", "/search", content=b'{"q":1}')
		assert r.headers["x-cache"] == "PASS"
	assert len(fake_upstream.requests) == 2
