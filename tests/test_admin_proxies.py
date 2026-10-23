# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Proxies admin page and its JSON mirror. Passwords are write-only, as tokens are."""

from proxy import db

SECRET = "pw-secret-7"
NEW = {"name": "corp-egress", "url": "http://10.0.0.5:3128", "username": "egress", "password": SECRET, "verify_tls_sent": "1"}


def test_nav_has_proxies_between_identities_and_approvals(client):
	text = client.get("/_admin/proxies").text
	assert text.index(">Identities<") < text.index(">Proxies<") < text.index(">Approvals<")
	assert "Pick it in the" in text


def test_add_via_page(client, admin_db, proxy_secrets):
	r = client.post("/_admin/proxies", data=NEW, follow_redirects=False)
	assert r.status_code == 303 and "added proxy" in client.get(r.headers["location"]).text
	assert db.get_proxy(admin_db, "corp-egress").username == "egress"
	assert proxy_secrets.get("corp-egress") == SECRET
	assert SECRET not in "\n".join(admin_db.iterdump())


def test_add_via_api_and_list(client):
	r = client.post("/_admin/api/proxies", json={"name": "gw", "url": "socks5h://127.0.0.1:1080"})
	assert r.status_code == 201
	assert r.json() == {
		"name": "gw", "url": "socks5h://127.0.0.1:1080", "username": None, "verify_tls": True,
		"ca_bundle": None, "password": "none", "keychain_error": None, "used_by": [],
	}
	assert [p["name"] for p in client.get("/_admin/api/proxies").json()["proxies"]] == ["gw"]


def test_refused_add_never_echoes_password_or_userinfo_url(client):
	r = client.post("/_admin/proxies", data={**NEW, "url": "http://egress:hunter2@10.0.0.5:3128"})
	assert r.status_code == 400
	assert "Username/Password" in r.text
	assert "hunter2" not in r.text and SECRET not in r.text
	assert 'value="corp-egress"' in r.text                      # the other fields come back as typed


def test_duplicate_and_bad_scheme_are_400(client):
	client.post("/_admin/proxies", data=NEW)
	assert client.post("/_admin/api/proxies", json={**NEW}).status_code == 400
	r = client.post("/_admin/api/proxies", json={"name": "x", "url": "ftp://h:21"})
	assert r.status_code == 400 and "unsupported proxy scheme" in r.json()["error"]


def test_page_shows_password_state_and_used_by(client, add_proxy):
	add_proxy()
	add_proxy(name="no-pw", password=None, route=())
	add_proxy(name="open", username=None, password=None, route=())
	text = client.get("/_admin/proxies").text
	assert 'placeholder="stored in Keychain"' in text
	assert "⚠️ missing from Keychain" in text
	assert '<a href="/_admin/upstreams#upstream-jira"><code>jira</code></a>' in text
	assert "pw-1" not in text


def test_edit_keeps_password_on_blank_and_reaches_the_next_request(client, add_proxy, proxy_secrets, admin_db):
	add_proxy()
	r = client.post("/_admin/proxies/corp-egress", data={"url": "http://10.0.0.6:3128", "username": "egress", "password": "", "verify_tls_sent": "1"}, follow_redirects=False)
	assert r.status_code == 303
	assert proxy_secrets.get("corp-egress") == "pw-1"
	client.put("/_admin/api/proxies/corp-egress", json={"url": "http://10.0.0.6:3128", "username": "egress", "password": "pw-2"})
	client.get("/x")
	assert client.app.state.proxies.route("corp-egress").password == "pw-2"


def test_verify_tls_checkbox_disabled_for_http(client, add_proxy):
	add_proxy()
	add_proxy(name="tls", url="https://p.test:443", route=())
	text = client.get("/_admin/proxies").text
	assert text.count('only https:// proxies have a certificate to verify') == 1


def test_delete_in_use_is_refused_and_button_disabled(client, add_proxy):
	add_proxy()
	assert 'disabled title="used by jira"' in client.get("/_admin/proxies").text
	r = client.post("/_admin/proxies/corp-egress/delete")
	assert r.status_code == 400 and "/" in r.text
	assert client.delete("/_admin/api/proxies/corp-egress").status_code == 400


def test_delete_unused(client, add_proxy, proxy_secrets):
	add_proxy(route=())
	r = client.delete("/_admin/api/proxies/corp-egress")
	assert r.json() == {"deleted": True, "name": "corp-egress", "keychain_item": True}
	assert proxy_secrets.get("corp-egress") is None
	assert client.delete("/_admin/api/proxies/corp-egress").status_code == 404


def test_editing_http_to_https_keeps_tls_verification_on(client, add_proxy, admin_db):
	add_proxy()
	text = client.get("/_admin/proxies").text
	assert text.count('name="verify_tls_sent"') == 1                # only the add form's; the http row sends none
	r = client.post("/_admin/proxies/corp-egress", data={"url": "https://p.test:443", "username": "egress", "password": ""}, follow_redirects=False)
	assert r.status_code == 303
	assert db.get_proxy(admin_db, "corp-egress").verify_tls is True
	assert client.get("/_admin/proxies").text.count('name="verify_tls_sent"') == 2   # the https row now carries it


def test_api_edit_without_username_keeps_auth(client, add_proxy, proxy_secrets, admin_db):
	import certifi
	add_proxy(url="https://10.0.0.5:3128", ca_bundle=certifi.where())
	r = client.put("/_admin/api/proxies/corp-egress", json={"url": "https://10.0.0.6:3128"})
	assert r.status_code == 200
	assert proxy_secrets.get("corp-egress") == "pw-1"
	row = db.get_proxy(admin_db, "corp-egress")
	assert row.url == "https://10.0.0.6:3128" and row.username == "egress" and row.ca_bundle == certifi.where()


def test_api_edit_with_null_username_clears_auth_and_password(client, add_proxy, proxy_secrets, admin_db):
	add_proxy()
	r = client.put("/_admin/api/proxies/corp-egress", json={"url": "http://10.0.0.5:3128", "username": None})
	assert r.status_code == 200
	assert db.get_proxy(admin_db, "corp-egress").username is None
	assert proxy_secrets.get("corp-egress") is None


def test_api_edit_with_empty_ca_bundle_clears_it(client, add_proxy, admin_db):
	import certifi
	add_proxy(url="https://10.0.0.5:3128", ca_bundle=certifi.where())
	r = client.put("/_admin/api/proxies/corp-egress", json={"url": "https://10.0.0.5:3128", "ca_bundle": ""})
	assert r.status_code == 200
	assert db.get_proxy(admin_db, "corp-egress").ca_bundle is None
