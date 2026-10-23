# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Test a URL: which rule wins and why, the key, and fresh/stale/absent — without ever fetching."""

import pytest

from proxy import db
from proxy.cache import ANONYMOUS, build_key, normalize_query
from proxy.rules import Rule, RuleSet
from tests.test_caching import shift_clock

ID = {"X-WR-Identity": "jira-uat"}


def test_match_all_orders_most_specific_first():
	rules = RuleSet([
		Rule(1, "jira", "/**", 3600),
		Rule(2, "jira", "/rest/api/2/issue/*", 60),
		Rule(3, "jira", "/rest/**", 600),
		Rule(4, "jira", "/rest/api/2/issue/ABC-1", 0, enabled=False),
		Rule(5, "wiki", "/rest/api/2/issue/ABC-1", 1),
	])
	matches = rules.match_all("jira", "/rest/api/2/issue/ABC-1")
	assert [r.id for r in matches] == [2, 3, 1]
	assert rules.match("jira", "/rest/api/2/issue/ABC-1") is matches[0]
	assert rules.match_all("wiki", "/nothing") == []


@pytest.fixture
def ruleset(admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600)
	db.create_rule(admin_db, "jira", "/rest/api/2/issue/*", 60, query_mode="ignore")
	db.create_rule(admin_db, "jira", "/rest/api/2/myself", 0)
	db.create_rule(admin_db, "jira", "/search", 600, query_mode="reject")


def test_ranked_matches_and_key(client, ruleset, fake_upstream):
	r = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/rest/api/2/issue/ABC-1", "query": "fields=summary"})
	assert r.status_code == 200
	body = r.json()
	assert [m["pattern"] for m in body["matches"]] == ["/rest/api/2/issue/*", "/**"]
	assert body["matches"][0] == {"id": 2, "pattern": "/rest/api/2/issue/*", "ttl": 60, "literals": 18, "wildcards": 1}
	assert body["upstream"] == "jira"
	assert body["identity"] == ANONYMOUS
	assert body["key"] == build_key(ANONYMOUS, "/rest/api/2/issue/ABC-1", normalize_query("fields=summary", "ignore"))
	assert body["state"] == "absent" and body["age"] is None
	assert "cacheable under rule /rest/api/2/issue/* (1m)" == body["outcome"]
	assert fake_upstream.requests == []


def test_include_mode_key_sorts_the_query(client, ruleset):
	body = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x", "query": "b=2&a=1"}).json()
	assert body["key"] == build_key(ANONYMOUS, "/x", "a=1&b=2")


def test_fresh_then_stale(client, ruleset, monkeypatch, fake_upstream):
	client.get("/rest/api/2/issue/ABC-1")
	body = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/rest/api/2/issue/ABC-1"}).json()
	assert body["state"] == "fresh" and body["age"] >= 0
	shift_clock(monkeypatch, 61)
	body = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/rest/api/2/issue/ABC-1"}).json()
	assert body["state"] == "stale" and body["age"] >= 61
	assert len(fake_upstream.requests) == 1   # the page never fetched


def test_identity_partition(client, ruleset, add_identity):
	identity = add_identity()
	client.get("/x", headers=ID)
	mine = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x", "identity": "jira-uat"}).json()
	assert mine["identity"] == "jira-uat"
	assert mine["key"] == build_key(identity.cache_identity, "/x", "")
	assert mine["state"] == "fresh"
	anon = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x"}).json()
	assert anon["state"] == "absent"
	assert anon["key"] != mine["key"]


@pytest.mark.parametrize("params, fragment", [
	({"upstream": "jira", "path": "/rest/api/2/myself"}, "bypass"),
	({"upstream": "jira", "path": "/search", "query": "q=1"}, "rejects queries"),
	({"upstream": "jira", "path": "/search"}, "cacheable under rule /search (10m)"),
	({"upstream": "nope", "path": "/x"}, "the request would get a 400: unknown upstream 'nope'; configured upstreams: jira"),
	({"path": "/x"}, "the request would get a 400: send X-WR-Upstream to choose an upstream"),
	({"upstream": "jira", "path": "/x", "identity": "ghost"}, None),
])
def test_outcomes(client, ruleset, params, fragment):
	r = client.get("/_admin/api/test", params=params)
	if fragment is None:
		assert r.status_code == 400
		assert "unknown identity" in r.json()["error"]
	else:
		assert r.status_code == 200
		assert fragment in r.json()["outcome"]
		assert r.json()["key"] is None or "cacheable" in fragment


def test_no_rule_matches(client, admin_db):
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	db.create_rule(admin_db, "wiki", "/**", 10)   # another upstream's rule never matches
	body = client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x"}).json()
	assert body["matches"] == []
	assert "no rule matches" in body["outcome"]


def test_page_renders_form_and_result(client, ruleset, add_identity):
	add_identity()
	empty = client.get("/_admin/test")
	assert empty.status_code == 200
	assert 'name="path"' in empty.text and '<option value="jira-uat">' in empty.text
	html = client.get("/_admin/test", params={"upstream": "jira", "path": "/rest/api/2/issue/ABC-1"}).text
	assert "/rest/api/2/issue/*" in html and "winner" in html
	assert "absent" in html
	assert build_key(ANONYMOUS, "/rest/api/2/issue/ABC-1", "") in html
	bad = client.get("/_admin/test", params={"upstream": "jira", "path": "/x", "identity": "ghost"})
	assert bad.status_code == 400 and 'class="flash error"' in bad.text
	assert client.get("/_admin/test", params={"upstream": "jira", "path": "rest/x"}).status_code == 400
	assert '<option value="jira">' in empty.text and "infer from the identity" in empty.text


def test_route_is_shown(client, add_proxy):
	assert client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x"}).json()["route"] == "Direct"
	add_proxy()
	assert client.get("/_admin/api/test", params={"upstream": "jira", "path": "/x"}).json()["route"] == "via corp-egress (http://10.0.0.5:3128)"
	assert "via corp-egress (http://10.0.0.5:3128)" in client.get("/_admin/test", params={"upstream": "jira", "path": "/x"}).text
	assert client.get("/_admin/api/test", params={"upstream": "nope", "path": "/x"}).json()["route"] is None


def test_the_upstream_is_inferred_from_an_identity_bound_to_one(client, ruleset, add_identity):
	add_identity()
	body = client.get("/_admin/api/test", params={"path": "/search", "identity": "jira-uat"}).json()
	assert body["upstream"] == "jira" and "cacheable under rule /search" in body["outcome"]


def test_routing_refusals_are_the_outcome(client, admin_db, ruleset, add_identity):
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	add_identity()
	body = client.get("/_admin/api/test", params={"upstream": "wiki", "path": "/x", "identity": "jira-uat"}).json()
	assert body["outcome"] == "the request would get a 403: identity 'jira-uat' is not bound to upstream 'wiki'; bound to: jira"
	assert body["upstream"] is None and body["matches"] == [] and body["key"] is None
	add_identity(name="both", upstream_id=["jira", "wiki"])
	body = client.get("/_admin/api/test", params={"path": "/x", "identity": "both"}).json()
	assert "400: identity 'both' is bound to several upstreams" in body["outcome"]
