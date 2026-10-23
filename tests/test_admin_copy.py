# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Copy rules from one upstream to another: the page, the API, and the copied/skipped report."""

import pytest

from proxy import db


@pytest.fixture
def two_upstreams(admin_db):
	db.put_upstream(admin_db, db.Upstream("jira-prod", "https://jira-prod.test"))
	db.create_rule(admin_db, "jira", "/rest/**", 3600, query_mode="ignore", note="rest")
	db.create_rule(admin_db, "jira", "/rest/api/2/myself", 0)
	db.create_rule(admin_db, "jira", "/off/*", 5, enabled=False)
	db.create_rule(admin_db, "jira-prod", "/rest/**", 60)   # already present on the target


def test_form_lists_upstreams(client, two_upstreams):
	html = client.get("/_admin/rules/copy").text
	assert '<option value="jira">' in html and '<option value="jira-prod">' in html
	assert 'name="source"' in html and 'name="target"' in html


def test_copy_via_page_reports_copied_and_skipped(client, two_upstreams, admin_db):
	r = client.post("/_admin/rules/copy", data={"source": "jira", "target": "jira-prod"})
	assert r.status_code == 200
	assert "/rest/api/2/myself" in r.text and "/off/*" in r.text
	assert "Skipped, already on jira-prod" in r.text and "Copied" in r.text
	rules = {(rule.upstream_id, rule.pattern) for rule in db.list_rules(admin_db)}
	assert {("jira-prod", "/rest/api/2/myself"), ("jira-prod", "/off/*")} <= rules
	assert len(rules) == 6


def test_copy_via_api(client, two_upstreams):
	r = client.post("/_admin/api/rules/copy", json={"source": "jira", "target": "jira-prod"})
	assert r.status_code == 200
	assert r.json() == {
		"source": "jira", "target": "jira-prod",
		"copied": ["/rest/api/2/myself", "/off/*"], "skipped": ["/rest/**"],
	}


@pytest.mark.parametrize("body, fragment", [
	({"source": "jira", "target": "jira"}, "same"),
	({"source": "jira", "target": "nope"}, "nope"),
	({"source": "", "target": "jira-prod"}, "source"),
])
def test_refusals(client, two_upstreams, body, fragment):
	r = client.post("/_admin/api/rules/copy", json=body)
	assert r.status_code == 400
	assert fragment in r.json()["error"]
	r = client.post("/_admin/rules/copy", data=body)
	assert r.status_code == 400
	assert 'class="flash error"' in r.text


def test_copied_rules_are_live_on_the_next_request(client, two_upstreams):
	prod = {"X-WR-Upstream": "jira-prod"}
	assert client.get("/rest/api/2/myself", headers=prod).headers["x-cache"] == "MISS"   # jira-prod's /rest/** at 60s
	assert client.get("/rest/api/2/myself", headers=prod).headers["x-cache"] == "HIT"
	client.post("/_admin/api/rules/copy", json={"source": "jira", "target": "jira-prod"})
	assert client.get("/rest/api/2/myself", headers=prod).headers["x-cache"] == "PASS"   # the copied bypass wins
