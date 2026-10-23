# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The cache browser: rows with live hit deltas, the identity filter, and the four purges."""

import pytest

from proxy import db
from proxy.admin_router import age_human, bytes_human
from proxy.cache import ANONYMOUS, build_key
from tests.test_caching import shift_clock

ID = {"X-WR-Identity": "jira-uat"}


@pytest.mark.parametrize("n, text", [(0, "0 B"), (999, "999 B"), (1536, "1.5 KB"), (3 * 1024 * 1024, "3.0 MB"), (2 * 1024 ** 3, "2.0 GB")])
def test_bytes_human(n, text):
	assert bytes_human(n) == text


@pytest.mark.parametrize("s, text", [(0, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"), (90000, "1d")])
def test_age_human(s, text):
	assert age_human(s) == text


@pytest.fixture
def warmed(client, admin_db, add_identity):
	"""Two shelves, three partitions, a few rules; some hits only in memory."""
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	identity = add_identity()
	broad = db.create_rule(admin_db, "jira", "/**", 3600)
	narrow = db.create_rule(admin_db, "jira", "/issue/*", 60)
	wiki = db.create_rule(admin_db, "wiki", "/**", 3600)
	client.get("/x")
	client.get("/x")                        # anonymous /jira/x: 1 live hit
	client.get("/issue/A")
	client.get("/issue/A", headers=ID)      # the identity's own entry
	client.get("/issue/A", headers=ID)      # 1 live hit
	client.get("/page", headers={"X-WR-Upstream": "wiki"})
	return {"identity": identity, "rules": {"broad": broad, "narrow": narrow, "wiki": wiki}}


def rows(client, **params):
	r = client.get("/_admin/api/cache", params=params)
	assert r.status_code == 200
	return r.json()


def test_browse_merges_live_hit_deltas(client, warmed):
	body = rows(client, upstream_id="jira")
	by_url = {(e["url"], e["identity"]): e for e in body["entries"]}
	assert by_url[("/x", ANONYMOUS)]["hits"] == 1
	assert by_url[("/issue/A", warmed["identity"].cache_identity)]["hits"] == 1
	assert by_url[("/issue/A", ANONYMOUS)]["hits"] == 0
	assert by_url[("/x", ANONYMOUS)]["rule"] == "/**"
	assert by_url[("/issue/A", ANONYMOUS)]["rule"] == "/issue/*"
	assert body["upstream_id"] == "jira"
	assert set(body["upstream_ids"]) == {"jira", "wiki"}
	assert body["count"] == 3 and body["bytes"] > 0
	entry = by_url[("/x", ANONYMOUS)]
	assert entry["key"] == build_key(ANONYMOUS, "/x", "")
	assert entry["fresh"] is True and entry["ttl"] == 3600 and entry["bytes"] > 256


def test_identity_filter_hides_other_partitions(client, warmed):
	partition = warmed["identity"].cache_identity
	body = rows(client, upstream_id="jira", identity=partition)
	assert {e["identity"] for e in body["entries"]} == {partition}
	assert body["identity"] == partition
	anon = rows(client, upstream_id="jira", identity=ANONYMOUS)
	assert {e["url"] for e in anon["entries"]} == {"/x", "/issue/A"}
	assert set(body["identities"]) >= {"all", ANONYMOUS, partition}


def test_default_prefix_is_the_first_upstream(client, warmed):
	assert rows(client)["upstream_id"] == "jira"


def test_stale_rows_are_chipped(client, warmed, monkeypatch):
	shift_clock(monkeypatch, 61)
	html = client.get("/_admin/cache", params={"upstream_id": "jira"}).text
	assert 'class="chip stale"' in html and 'class="chip fresh"' in html
	assert '/issue/A' in html
	body = rows(client, upstream_id="jira")
	states = {e["url"]: e["fresh"] for e in body["entries"] if e["identity"] == ANONYMOUS}
	assert states == {"/x": True, "/issue/A": False}


def test_page_renders_selects_and_table(client, warmed):
	html = client.get("/_admin/cache", params={"upstream_id": "jira"}).text
	assert "<h1>Cache</h1>" in html
	assert '<option value="jira" selected>' in html and '<option value="wiki">' in html
	assert f'<option value="{warmed["identity"].cache_identity}">' in html
	assert 'id="entries"' in html
	assert "/issue/A" in html


def test_purge_entry_via_page_returns_the_partial(client, warmed):
	key = build_key(ANONYMOUS, "/x", "")
	r = client.post("/_admin/cache/purge", data={"upstream_id": "jira", "key": key})
	assert r.status_code == 200
	assert r.text.lstrip().startswith('<div id="entries"')
	assert "purged 1" in r.text
	assert "/x" not in r.text
	assert client.get("/x").headers["x-cache"] == "MISS"


def test_purge_entry_via_api(client, warmed):
	key = build_key(ANONYMOUS, "/x", "")
	assert client.delete(f"/_admin/api/cache/jira/entries/{key}").json() == {"purged": 1}
	assert client.delete(f"/_admin/api/cache/jira/entries/{key}").status_code == 404


def test_purge_by_rule_spans_shelves(client, warmed):
	narrow = warmed["rules"]["narrow"]
	assert client.delete(f"/_admin/api/cache/rules/{narrow.id}").json() == {"purged": 2}
	assert {e["url"] for e in rows(client, upstream_id="jira")["entries"]} == {"/x"}
	r = client.post("/_admin/cache/purge", data={"upstream_id": "wiki", "rule_id": str(warmed["rules"]["wiki"].id)})
	assert "purged 1" in r.text
	assert rows(client, upstream_id="wiki")["entries"] == []


def test_purge_by_identity_via_page_and_api(client, warmed):
	partition = warmed["identity"].cache_identity
	r = client.post("/_admin/cache/purge", data={"upstream_id": "jira", "identity": partition})
	assert "purged 1" in r.text
	assert client.delete(f"/_admin/api/cache/identities/{ANONYMOUS}").json() == {"purged": 3}   # both shelves
	assert rows(client, upstream_id="jira")["entries"] == [] and rows(client, upstream_id="wiki")["entries"] == []


def test_purge_all_via_page_and_api(client, warmed):
	r = client.post("/_admin/cache/purge", data={"upstream_id": "jira", "all": "1"})
	assert "purged 3" in r.text
	assert rows(client, upstream_id="jira")["entries"] == []
	assert client.delete("/_admin/api/cache/wiki").json() == {"purged": 1}
	assert client.delete("/_admin/api/cache/wiki").json() == {"purged": 0}
	assert client.get("/x").headers["x-cache"] == "MISS"


def test_purge_needs_exactly_one_target(client, warmed):
	for data in ({"upstream_id": "jira"}, {"upstream_id": "jira", "all": "1", "key": "k"}, {"upstream_id": "jira", "rule_id": "x"}):
		r = client.post("/_admin/cache/purge", data=data)
		assert r.status_code == 400
		assert r.text.lstrip().startswith('<div id="entries"') and "error:" in r.text   # the partial, so htmx shows it
	assert rows(client, upstream_id="jira")["count"] == 3


def test_confirm_text_is_data_not_script(client, admin_db):
	db.create_rule(admin_db, "jira", "/it's-here/*", 60)
	html = client.get("/_admin/rules").text
	assert "data-confirm=\"Delete /it&#39;s-here/* on jira?\"" in html
	assert "confirm('" not in html


def test_a_traversing_upstream_id_never_opens_a_shelf(client, settings):
	assert client.get("/_admin/api/cache", params={"upstream_id": "../../escaped"}).status_code == 400
	assert client.get("/_admin/cache", params={"upstream_id": "../../escaped"}).status_code == 400
	assert client.post("/_admin/cache/purge", data={"upstream_id": "../../escaped", "all": "1"}).status_code == 400
	assert client.delete("/_admin/api/cache/..%2F..%2Fescaped").status_code in (400, 404)
	assert client.delete("/_admin/api/cache/..%2Fescaped/entries/k").status_code in (400, 404)
	assert not list(settings.cache_dir.parent.parent.glob("escaped*"))
	assert not list(settings.cache_dir.parent.glob("escaped*"))
