# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Rules admin pages, the JSON mirror, and the version counter reaching the proxy."""

import pytest

from proxy import db
from proxy.admin_router import TTL_PRESETS, parse_ttl, ttl_human


@pytest.mark.parametrize("preset, custom, expected", [
	("never", "", None),
	("bypass", "", 0),
	("3600", "", 3600),
	("custom", "90s", 90),
	("custom", "45m", 2700),
	("custom", " 3H ", 10800),
	("custom", "2d", 172800),
])
def test_parse_ttl(preset, custom, expected):
	assert parse_ttl(preset, custom) == expected


@pytest.mark.parametrize("preset, custom", [("custom", ""), ("custom", "soon"), ("custom", "5w"), ("1x", "")])
def test_parse_ttl_refuses(preset, custom):
	with pytest.raises(ValueError):
		parse_ttl(preset, custom)


@pytest.mark.parametrize("ttl, text", [
	(None, "never expires"), (0, "bypass"), (60, "1m"), (120, "2m"), (600, "10m"),
	(3600, "1h"), (32400, "9h"), (86400, "1d"), (90, "90s"), (5400, "90m"),
])
def test_ttl_human(ttl, text):
	assert ttl_human(ttl) == text


def test_presets_round_trip():
	for label, seconds in TTL_PRESETS:
		assert ttl_human(seconds) == label
		assert parse_ttl(str(seconds)) == seconds


# --- pages ----------------------------------------------------------------------

def test_list_shows_rules_with_human_ttl(client, admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600, note="all of jira")
	db.create_rule(admin_db, "jira", "/rest/api/2/myself", 0)
	html = client.get("/_admin/rules").text
	assert "/**" in html and "1h" in html and "all of jira" in html
	assert "bypass" in html
	assert "No rules yet" not in html


def test_new_form_renders(client):
	html = client.get("/_admin/rules/new").text
	assert '<form method="post" action="/_admin/rules"' in html
	for label, _ in TTL_PRESETS:
		assert f">{label}<" in html or f"{label}</label>" in html
	assert 'name="query_mode"' in html


def test_edit_form_is_prefilled(client, admin_db):
	rule = db.create_rule(admin_db, "jira", "/**", 90, query_mode="ignore", note="hello")
	html = client.get(f"/_admin/rules/{rule.id}/edit").text
	assert f'action="/_admin/rules/{rule.id}"' in html
	assert 'value="/**"' in html
	assert 'value="custom" checked' in html and 'value="90s"' in html
	assert 'value="ignore" checked' in html
	assert 'value="hello"' in html
	assert client.get("/_admin/rules/999/edit").status_code == 404


def test_create_via_page(client, admin_db):
	r = client.post(
		"/_admin/rules",
		data={"upstream_id": "jira", "pattern": "/rest/**", "ttl": "custom", "ttl_custom": "45m", "query_mode": "reject", "note": "api"},
		follow_redirects=False,
	)
	assert r.status_code == 303
	assert r.headers["location"].startswith("/_admin/rules")
	[rule] = db.list_rules(admin_db)
	assert (rule.upstream_id, rule.pattern, rule.ttl_seconds, rule.query_mode, rule.enabled, rule.note) == ("jira", "/rest/**", 2700, "reject", True, "api")


def test_create_via_api(client, admin_db):
	r = client.post("/_admin/api/rules", json={"upstream_id": "jira", "pattern": "/**", "ttl_seconds": None, "query_mode": "include"})
	assert r.status_code == 201
	assert r.json()["ttl_seconds"] is None and r.json()["ttl"] == "never expires" and r.json()["hits"] == 0
	assert r.json()["upstream_id"] == "jira"
	r = client.post("/_admin/api/rules", json={"upstream_id": "jira", "pattern": "/x", "ttl": "custom", "ttl_custom": "2d"})
	assert r.status_code == 201
	assert r.json()["ttl_seconds"] == 172800
	assert len(db.list_rules(admin_db)) == 2


def test_duplicate_pattern_shows_the_flash(client, admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600)
	r = client.post("/_admin/rules", data={"upstream_id": "jira", "pattern": "/**", "ttl": "3600", "query_mode": "include"})
	assert r.status_code == 400
	assert 'class="flash error"' in r.text and "UNIQUE" in r.text
	assert 'value="/**"' in r.text and '<option value="jira" selected>' in r.text   # the form comes back filled in
	r = client.post("/_admin/api/rules", json={"upstream_id": "jira", "pattern": "/**", "ttl_seconds": 1})
	assert r.status_code == 400


def test_bad_pattern_shows_the_flash(client):
	r = client.post("/_admin/rules", data={"upstream_id": "jira", "pattern": "rest/**", "ttl": "3600", "query_mode": "include"})
	assert r.status_code == 400
	assert "must start with" in r.text


def test_update_via_page_and_api(client, admin_db):
	rule = db.create_rule(admin_db, "jira", "/**", 3600)
	r = client.post(f"/_admin/rules/{rule.id}", data={"pattern": "/rest/**", "ttl": "never", "query_mode": "ignore", "enabled": "on", "note": "x"}, follow_redirects=False)
	assert r.status_code == 303
	updated = db.get_rule(admin_db, rule.id)
	assert (updated.pattern, updated.ttl_seconds, updated.query_mode, updated.enabled, updated.note) == ("/rest/**", None, "ignore", True, "x")
	r = client.put(f"/_admin/api/rules/{rule.id}", json={"pattern": "/**", "ttl_seconds": 5, "query_mode": "include", "enabled": False, "note": None})
	assert r.status_code == 200
	assert r.json()["enabled"] is False
	assert db.get_rule(admin_db, rule.id).enabled is False
	assert client.put("/_admin/api/rules/999", json={"pattern": "/x", "ttl_seconds": 5}).status_code == 404


def test_delete_via_page_and_api(client, admin_db):
	a = db.create_rule(admin_db, "jira", "/a", 1)
	b = db.create_rule(admin_db, "jira", "/b", 1)
	assert client.post(f"/_admin/rules/{a.id}/delete", follow_redirects=False).status_code == 303
	assert client.delete(f"/_admin/api/rules/{b.id}").json() == {"deleted": True, "id": b.id}
	assert client.delete(f"/_admin/api/rules/{b.id}").status_code == 404
	assert client.post("/_admin/rules/999/delete").status_code == 404
	assert db.list_rules(admin_db) == []


def test_toggle_returns_the_row_and_flips_matching(client, admin_db):
	rule = db.create_rule(admin_db, "jira", "/**", 3600)
	assert client.get("/x").headers["x-cache"] == "MISS"
	r = client.post(f"/_admin/rules/{rule.id}/toggle")
	assert r.status_code == 200
	assert r.text.strip().startswith(f'<tr id="rule-{rule.id}">')
	assert 'class="toggle off"' in r.text
	assert db.get_rule(admin_db, rule.id).enabled is False
	assert client.get("/x").headers["x-cache"] == "PASS"
	assert 'class="toggle on"' in client.post(f"/_admin/rules/{rule.id}/toggle").text
	assert client.get("/x").headers["x-cache"] == "HIT"
	assert client.post("/_admin/rules/999/toggle").status_code == 404


def test_rule_edit_reaches_the_running_proxy(client, fake_upstream):
	rule = client.post("/_admin/api/rules", json={"upstream_id": "jira", "pattern": "/**", "ttl_seconds": 3600}).json()
	assert client.get("/x").headers["x-cache"] == "MISS"
	assert client.get("/x").headers["x-cache"] == "HIT"
	client.put(f"/_admin/api/rules/{rule['id']}", json={"pattern": "/**", "ttl_seconds": 0})
	assert client.get("/x").headers["x-cache"] == "PASS"


def test_hit_counts_on_the_page_and_api(client, admin_db):
	broad = db.create_rule(admin_db, "jira", "/**", 3600)
	narrow = db.create_rule(admin_db, "jira", "/issue/*", 3600)
	for _ in range(3):
		client.get("/x")            # broad: 1 miss, 2 hits
	for _ in range(2):
		client.get("/issue/A")      # narrow: 1 miss, 1 hit
	client.get("/issue/B")          # narrow: a miss
	rows = {row["id"]: row for row in client.get("/_admin/api/rules").json()["rules"]}
	assert rows[broad.id]["hits"] == 2
	assert rows[narrow.id]["hits"] == 1
	html = client.get("/_admin/rules").text
	assert '<td class="num">2</td>' in html and '<td class="num">1</td>' in html


@pytest.mark.parametrize("upstream_id, fragment", [(None, "upstream_id is required"), ("nope", "no upstream 'nope'")])
def test_a_rule_needs_a_known_upstream(client, admin_db, upstream_id, fragment):
	body = {"pattern": "/**", "ttl_seconds": 60} | ({"upstream_id": upstream_id} if upstream_id else {})
	r = client.post("/_admin/api/rules", json=body)
	assert r.status_code == 400 and fragment in r.json()["error"]
	r = client.post("/_admin/rules", data={"pattern": "/**", "ttl": "60", "query_mode": "include"} | ({"upstream_id": upstream_id} if upstream_id else {}))
	assert r.status_code == 400 and fragment.replace("'", "&#39;") in r.text
	assert db.list_rules(admin_db) == []


def test_an_edit_may_move_a_rule_and_omitting_the_upstream_keeps_it(client, admin_db):
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	rule = db.create_rule(admin_db, "jira", "/**", 3600)
	r = client.put(f"/_admin/api/rules/{rule.id}", json={"pattern": "/**", "ttl_seconds": 5})
	assert r.status_code == 200 and r.json()["upstream_id"] == "jira"
	r = client.put(f"/_admin/api/rules/{rule.id}", json={"upstream_id": "wiki", "pattern": "/**", "ttl_seconds": 5})
	assert r.json()["upstream_id"] == "wiki"
	assert client.get("/x").headers["x-cache"] == "PASS"   # no longer a jira rule
	assert client.get("/x", headers={"X-WR-Upstream": "wiki"}).headers["x-cache"] == "MISS"


def test_the_list_filters_by_upstream_and_the_form_offers_them(client, admin_db):
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	db.create_rule(admin_db, "jira", "/jira-only", 60)
	db.create_rule(admin_db, "wiki", "/wiki-only", 60)
	html = client.get("/_admin/rules").text
	assert "/jira-only" in html and "/wiki-only" in html
	html = client.get("/_admin/rules", params={"upstream": "wiki"}).text
	assert "/wiki-only" in html and "/jira-only" not in html
	assert '<option value="wiki" selected>' in html and 'href="/_admin/rules/new?upstream=wiki"' in html
	html = client.get("/_admin/rules/new", params={"upstream": "wiki"}).text
	assert 'name="upstream_id"' in html and '<option value="wiki" selected>' in html and '<option value="jira">' in html
