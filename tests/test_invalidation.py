# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A 2xx mutation purges the caller's own partition's entries for that exact path."""

import httpx
import pytest

from proxy import db

ID = {"X-WR-Identity": "jira-uat"}
OTHER = {"X-WR-Identity": "other"}


@pytest.fixture
def cached(admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600)


def test_mutation_purges_own_partition(client, cached, add_identity):
	add_identity()
	assert client.get("/issue/X", headers=ID).headers["x-cache"] == "MISS"
	assert client.get("/issue/X", headers=ID).headers["x-cache"] == "HIT"
	r = client.put("/issue/X", headers=ID, content=b"{}")
	assert r.status_code == 200
	assert r.headers["x-cache"] == "PASS"
	assert r.headers["x-cache-invalidated"] == "1"
	assert client.get("/issue/X", headers=ID).headers["x-cache"] == "MISS"


def test_mutation_purges_every_query_variant_of_the_path(client, cached):
	client.get("/search?q=1")
	client.get("/search?q=2")
	client.get("/search")
	client.get("/search/other")
	assert client.post("/search", content=b"{}").headers["x-cache-invalidated"] == "3"
	assert client.get("/search?q=1").headers["x-cache"] == "MISS"
	assert client.get("/search/other").headers["x-cache"] == "HIT"


def test_mutation_spares_other_identities(client, cached, add_identity):
	add_identity()
	add_identity(name="other", token="tok-2")
	client.get("/issue/X", headers=ID)
	client.get("/issue/X", headers=OTHER)
	client.get("/issue/X")
	assert client.post("/issue/X", headers=ID, content=b"{}").headers["x-cache-invalidated"] == "1"
	assert client.get("/issue/X", headers=OTHER).headers["x-cache"] == "HIT"
	assert client.get("/issue/X").headers["x-cache"] == "HIT"
	assert client.get("/issue/X", headers=ID).headers["x-cache"] == "MISS"


def test_non_2xx_mutation_purges_nothing(client, cached, fake_upstream):
	client.get("/issue/X")
	fake_upstream.responder = lambda req: httpx.Response(409, json={"error": "conflict"})
	r = client.patch("/issue/X", content=b"{}")
	assert r.status_code == 409
	assert "x-cache-invalidated" not in r.headers
	fake_upstream.responder = lambda req: httpx.Response(200, json={"ok": True})
	assert client.get("/issue/X").headers["x-cache"] == "HIT"


def test_anonymous_mutation_purges_anonymous(client, cached, fake_upstream):
	client.get("/issue/X")
	fake_upstream.responder = lambda req: httpx.Response(204)
	assert client.delete("/issue/X").headers["x-cache-invalidated"] == "1"
	fake_upstream.responder = lambda req: httpx.Response(200, json={"ok": True})
	assert client.get("/issue/X").headers["x-cache"] == "MISS"


def test_mutation_with_nothing_cached_reports_zero(client, cached):
	assert client.post("/issue/X", content=b"{}").headers["x-cache-invalidated"] == "0"


def test_get_and_head_never_invalidate(client, cached, admin_db):
	db.create_rule(admin_db, "jira", "/raw/*", 0)
	client.get("/raw/x")
	assert client.get("/raw/x").headers["x-cache"] == "PASS"
	assert "x-cache-invalidated" not in client.get("/raw/x").headers
	assert "x-cache-invalidated" not in client.head("/raw/x").headers
	assert "x-cache-invalidated" not in client.get("/issue/X").headers
