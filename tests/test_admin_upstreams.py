# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Upstreams admin page and its JSON mirror."""

from proxy import db


def test_list_page_shows_the_seeded_upstream(client):
	r = client.get("/_admin/upstreams")
	assert r.status_code == 200
	assert "<h1>Upstreams</h1>" in r.text
	assert "<code>jira</code>" in r.text and "https://jira.test" in r.text


def test_each_row_offers_its_x_wr_upstream_line_to_copy(client, admin_db):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	html = client.get("/_admin/upstreams").text
	assert '<code>X-WR-Upstream: jira</code> <button type="button" class="copy" data-copy="X-WR-Upstream: jira">copy</button>' in html
	assert 'data-copy="X-WR-Upstream: conf"' in html
	assert html.count('class="copy"') == 2


def test_api_list(client, add_identity):
	add_identity()
	r = client.get("/_admin/api/upstreams")
	assert r.json() == {"upstreams": [
		{"upstream_id": "jira", "base_url": "https://jira.test", "verify_tls": True, "timeout_s": 30.0, "identities": ["jira-uat"], "proxy": None, "plaintext_via_proxy": False},
	]}


def test_create_via_page(client, admin_db):
	r = client.post(
		"/_admin/upstreams",
		data={"upstream_id": "wiki", "base_url": "https://wiki.test/", "verify_tls": "on", "timeout_s": "5"},
		follow_redirects=False,
	)
	assert r.status_code == 303
	assert r.headers["location"].startswith("/_admin/upstreams")
	assert db.get_upstream(admin_db, "wiki") == db.Upstream("wiki", "https://wiki.test", verify_tls=True, timeout_s=5.0)
	followed = client.get(r.headers["location"])
	assert 'class="flash"' in followed.text and "saved upstream" in followed.text


def test_create_via_api(client, admin_db):
	r = client.post("/_admin/api/upstreams", json={"upstream_id": "wiki", "base_url": "https://wiki.test", "verify_tls": False, "timeout_s": 2.5})
	assert r.status_code == 201
	assert r.json() == {"upstream_id": "wiki", "base_url": "https://wiki.test", "verify_tls": False, "timeout_s": 2.5, "identities": [], "proxy": None, "plaintext_via_proxy": False}
	assert db.get_upstream(admin_db, "wiki").verify_tls is False


def test_edit_via_page_keeps_the_prefix(client, admin_db):
	client.post("/_admin/upstreams", data={"upstream_id": "jira", "base_url": "https://jira2.test", "timeout_s": "10"})
	up = db.get_upstream(admin_db, "jira")
	assert (up.base_url, up.verify_tls, up.timeout_s) == ("https://jira2.test", False, 10.0)   # unchecked box = off
	assert [u.upstream_id for u in db.list_upstreams(admin_db)] == ["jira"]


def test_edit_via_api(client, admin_db, fake_upstream):
	r = client.put("/_admin/api/upstreams/jira", json={"base_url": "https://jira3.test", "verify_tls": True, "timeout_s": 7})
	assert r.status_code == 200
	assert r.json()["base_url"] == "https://jira3.test"
	client.get("/x")
	assert fake_upstream.requests[-1].url.host == "jira3.test"   # the proxy reads upstreams per request


def test_api_edit_of_unknown_prefix_is_404(client):
	r = client.put("/_admin/api/upstreams/nope", json={"base_url": "https://x.test", "verify_tls": True, "timeout_s": 7})
	assert r.status_code == 404
	assert "nope" in r.json()["error"]


def test_delete_via_page_and_api(client, admin_db):
	db.put_upstream(admin_db, db.Upstream("wiki", "https://wiki.test"))
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	r = client.post("/_admin/upstreams/wiki/delete", follow_redirects=False)
	assert r.status_code == 303
	assert db.get_upstream(admin_db, "wiki") is None
	r = client.delete("/_admin/api/upstreams/conf")
	assert r.status_code == 200
	assert r.json() == {"deleted": True, "upstream_id": "conf"}
	assert client.delete("/_admin/api/upstreams/conf").status_code == 404


def test_delete_unbinds_its_identities_and_keeps_them(client, admin_db, add_identity, fake_upstream):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	add_identity(upstream_id=["jira", "conf"])
	assert client.delete("/_admin/api/upstreams/jira").status_code == 200
	assert db.get_identity(admin_db, "jira-uat").upstream_ids == ("conf",)
	assert client.get("/x", headers={"X-WR-Identity": "jira-uat", "X-WR-Upstream": "conf"}).status_code == 200
	r = client.post("/_admin/upstreams/conf/delete", follow_redirects=False)
	assert r.status_code == 303
	assert db.get_identity(admin_db, "jira-uat").upstream_ids == ()


def test_case_collision_refused(client, admin_db):
	r = client.post("/_admin/api/upstreams", json={"upstream_id": "Jira", "base_url": "https://x.test", "verify_tls": True, "timeout_s": 7})
	assert r.status_code == 400
	assert "collides" in r.json()["error"]
	assert [u.upstream_id for u in db.list_upstreams(admin_db)] == ["jira"]


def test_bad_timeout_renders_the_flash(client, admin_db):
	r = client.post("/_admin/upstreams", data={"upstream_id": "wiki", "base_url": "https://wiki.test", "timeout_s": "900"})
	assert r.status_code == 400
	assert 'class="flash error"' in r.text
	assert "timeout" in r.text
	assert db.get_upstream(admin_db, "wiki") is None
	r = client.post("/_admin/upstreams", data={"upstream_id": "wiki", "base_url": "https://wiki.test", "timeout_s": "soon"})
	assert r.status_code == 400


def test_bad_url_renders_the_flash(client, admin_db):
	r = client.post("/_admin/upstreams", data={"upstream_id": "wiki", "base_url": "ftp://wiki.test", "timeout_s": "30"})
	assert r.status_code == 400
	assert "base_url must be http" in r.text


# --- binding identities (identity first, then the upstream) ----------------------------

def test_page_offers_a_checkbox_per_identity(client, add_identity):
	add_identity()
	add_identity(name="svc", token="tok-s", upstream_id=None)
	html = client.get("/_admin/upstreams").text
	assert 'form="edit-jira" type="checkbox" name="identities" value="jira-uat" checked' in html
	assert 'form="edit-jira" type="checkbox" name="identities" value="svc">' in html
	assert html.count('name="identities_sent"') == 2   # the row's edit form and the Add form


def test_page_without_identities_says_to_create_one_first(client):
	html = client.get("/_admin/upstreams").text
	assert 'type="checkbox" name="identities"' not in html
	assert "create an identity first" in html.lower()


def test_add_binds_the_checked_identities(client, admin_db, add_identity, fake_upstream):
	add_identity(upstream_id=None)
	add_identity(name="svc", token="tok-s", upstream_id=None)
	r = client.post(
		"/_admin/upstreams",
		data={"upstream_id": "conf", "base_url": "https://conf.test", "timeout_s": "30",
			"identities_sent": "1", "identities": ["jira-uat", "svc"]},
		follow_redirects=False,
	)
	assert r.status_code == 303
	assert db.identities_by_upstream(admin_db)["conf"] == ["jira-uat", "svc"]
	assert client.get("/x", headers={"X-WR-Identity": "svc", "X-WR-Upstream": "conf"}).status_code == 200


def test_edit_with_every_box_unchecked_unbinds_all(client, admin_db, add_identity):
	add_identity()
	client.post("/_admin/upstreams", data={"upstream_id": "jira", "base_url": "https://jira.test", "timeout_s": "30", "identities_sent": "1"})
	assert db.identities_by_upstream(admin_db) == {}


def test_edit_with_one_box_checked(client, admin_db, add_identity):
	add_identity()
	add_identity(name="svc", token="tok-s", upstream_id=None)
	client.post("/_admin/upstreams", data={
		"upstream_id": "jira", "base_url": "https://jira.test", "timeout_s": "30", "identities_sent": "1", "identities": "svc",
	})
	assert db.identities_by_upstream(admin_db) == {"jira": ["svc"]}


def test_unknown_identity_is_refused_and_nothing_is_saved(client, admin_db, add_identity):
	add_identity()
	r = client.post("/_admin/upstreams", data={
		"upstream_id": "jira", "base_url": "https://jira2.test", "timeout_s": "30", "identities_sent": "1", "identities": "ghost",
	})
	assert r.status_code == 400
	assert 'class="flash error"' in r.text and "ghost" in r.text
	assert db.get_upstream(admin_db, "jira").base_url == "https://jira.test"
	assert db.identities_by_upstream(admin_db) == {"jira": ["jira-uat"]}


def test_api_sets_bindings_and_omitting_them_keeps_them(client, admin_db, add_identity):
	add_identity(upstream_id=None)
	r = client.post("/_admin/api/upstreams", json={
		"upstream_id": "conf", "base_url": "https://conf.test", "verify_tls": True, "timeout_s": 30, "identities": ["jira-uat"],
	})
	assert r.status_code == 201 and r.json()["identities"] == ["jira-uat"]
	r = client.put("/_admin/api/upstreams/conf", json={"base_url": "https://conf2.test", "verify_tls": True, "timeout_s": 30})
	assert r.json()["identities"] == ["jira-uat"]
	r = client.put("/_admin/api/upstreams/conf", json={"base_url": "https://conf2.test", "verify_tls": True, "timeout_s": 30, "identities": []})
	assert r.json()["identities"] == []
	r = client.put("/_admin/api/upstreams/conf", json={"base_url": "https://conf2.test", "verify_tls": True, "timeout_s": 30, "identities": "jira-uat"})
	assert r.status_code == 400 and "list" in r.json()["error"]


def test_delete_confirm_names_the_bound_identities(client, add_identity):
	add_identity()
	add_identity(name="svc", token="tok-s")
	html = client.get("/_admin/upstreams").text
	assert 'data-confirm="Delete upstream jira? Its rules are deleted too; its cache shelf is left on disk. This unbinds jira-uat, svc' in html


# --- outbound proxies --------------------------------------------------------------

def test_route_an_upstream_through_a_proxy_via_page(client, admin_db, add_proxy):
	add_proxy(route=())
	client.post("/_admin/upstreams", data={"upstream_id": "jira", "base_url": "https://jira.test", "verify_tls": "on", "timeout_s": "30", "proxy": "corp-egress"})
	assert db.get_upstream(admin_db, "jira").proxy == "corp-egress"
	text = client.get("/_admin/upstreams").text
	assert '<option value="corp-egress" selected>corp-egress</option>' in text
	client.post("/_admin/upstreams", data={"upstream_id": "jira", "base_url": "https://jira.test", "timeout_s": "30", "proxy": ""})
	assert db.get_upstream(admin_db, "jira").proxy is None


def test_unknown_proxy_is_400(client):
	r = client.put("/_admin/api/upstreams/jira", json={"base_url": "https://jira.test", "verify_tls": True, "timeout_s": 7, "proxy": "ghost"})
	assert r.status_code == 400 and "ghost" in r.json()["error"]


def test_api_edit_without_proxy_keeps_the_route(client, admin_db, add_proxy):
	add_proxy()
	r = client.put("/_admin/api/upstreams/jira", json={"base_url": "https://jira2.test", "verify_tls": True, "timeout_s": 7})
	assert r.json()["proxy"] == "corp-egress"
	r = client.put("/_admin/api/upstreams/jira", json={"base_url": "https://jira2.test", "verify_tls": True, "timeout_s": 7, "proxy": None})
	assert r.json()["proxy"] is None


def test_plaintext_warning_needs_http_and_a_proxy_and_an_identity(client, admin_db, add_proxy, add_identity):
	add_proxy()
	assert "can read the injected credentials" not in client.get("/_admin/upstreams").text   # https
	client.put("/_admin/api/upstreams/jira", json={"base_url": "http://jira.test", "verify_tls": True, "timeout_s": 7})
	assert client.get("/_admin/api/upstreams").json()["upstreams"][0]["plaintext_via_proxy"] is False   # no identity
	add_identity()
	assert client.get("/_admin/api/upstreams").json()["upstreams"][0]["plaintext_via_proxy"] is True
	assert "can read the injected credentials" in client.get("/_admin/upstreams").text
