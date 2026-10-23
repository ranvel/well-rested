# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Stale-if-error: an upstream outage serves the caller's own stale entry, never anyone else's."""

import httpx
import pytest

from proxy import db
from tests.test_caching import shift_clock

ID = {"X-WR-Identity": "jira-uat"}
STALE_WARNING = '110 - "response is stale"'


def refused(request):
	raise httpx.ConnectError("refused", request=request)


def timed_out(request):
	raise httpx.ConnectTimeout("slow", request=request)


@pytest.fixture
def short_ttl(admin_db):
	db.create_rule(admin_db, "jira", "/**", 60)


@pytest.fixture
def stale_entry(client, short_ttl, fake_upstream, monkeypatch):
	"""Warm /jira/x anonymously with a distinctive body, then let it go stale."""
	fake_upstream.responder = lambda req: httpx.Response(200, json={"from": "warm"})
	assert client.get("/x").headers["x-cache"] == "MISS"
	shift_clock(monkeypatch, 61)
	fake_upstream.requests.clear()


def test_stale_served_on_connect_error(client, stale_entry, fake_upstream):
	fake_upstream.responder = refused
	r = client.get("/x")
	assert r.status_code == 200
	assert r.json() == {"from": "warm"}
	assert r.headers["x-cache"] == "STALE"
	assert int(r.headers["age"]) >= 60
	assert r.headers.get_list("warning") == [STALE_WARNING]
	assert len(fake_upstream.requests) == 3   # the retries were exhausted first


def test_stale_served_on_final_503(client, stale_entry, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(503, text="maintenance")
	r = client.get("/x")
	assert r.status_code == 200
	assert r.json() == {"from": "warm"}
	assert r.headers["x-cache"] == "STALE"
	assert len(fake_upstream.requests) == 3


def test_500_is_passed_through_not_stale(client, stale_entry, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(500, text="origin bug")
	r = client.get("/x")
	assert r.status_code == 500
	assert r.text == "origin bug"
	assert r.headers["x-cache"] == "REFRESH"
	assert "warning" not in r.headers


@pytest.mark.parametrize("responder, status", [(refused, 502), (timed_out, 504)])
def test_error_with_no_entry_maps_to_502_or_504(client, short_ttl, fake_upstream, responder, status):
	fake_upstream.responder = responder
	r = client.get("/x")
	assert r.status_code == status
	assert "x-cache" not in r.headers


def test_stale_never_crosses_partitions(client, short_ttl, add_identity, fake_upstream, monkeypatch):
	add_identity()
	add_identity(name="other", token="tok-2")
	fake_upstream.responder = lambda req: httpx.Response(200, json={"from": "A"})
	assert client.get("/x", headers=ID).headers["x-cache"] == "MISS"
	shift_clock(monkeypatch, 61)
	fake_upstream.responder = refused
	assert client.get("/x", headers={"X-WR-Identity": "other"}).status_code == 502
	assert client.get("/x").status_code == 502
	stale = client.get("/x", headers=ID)
	assert stale.headers["x-cache"] == "STALE"
	assert stale.json() == {"from": "A"}


def test_refresh_still_works(client, stale_entry, fake_upstream):
	fake_upstream.responder = refused
	assert client.get("/x").headers["x-cache"] == "STALE"
	fake_upstream.responder = lambda req: httpx.Response(200, json={"from": "fresh"})
	r = client.get("/x")
	assert r.headers["x-cache"] == "REFRESH"
	assert r.json() == {"from": "fresh"}
	assert "warning" not in r.headers
	assert client.get("/x").headers["x-cache"] == "HIT"


def test_token_warning_rides_along_on_stale(client, short_ttl, add_identity, fake_upstream, monkeypatch):
	add_identity(days_left=2)
	client.get("/x", headers=ID)
	shift_clock(monkeypatch, 61)
	fake_upstream.responder = refused
	r = client.get("/x", headers=ID)
	assert r.headers["x-cache"] == "STALE"
	warnings = r.headers.get_list("warning")
	assert STALE_WARNING in warnings
	assert any(w.startswith("299 - \"token for 'jira-uat' expires ") for w in warnings)
	assert len(warnings) == 2
