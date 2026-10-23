# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Identities admin page: health bars, write-only tokens, add/rotate/delete through the shared helpers."""

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from proxy import db
from proxy.keychain import KeychainError

ID = {"X-WR-Identity": "jira-uat"}
SECRET = "tok-secret-7f3a9c"
ADD = {
	"name": "jira-uat", "scheme": "basic", "username": "ran@example.com",
	"token": SECRET, "expires": "30d",
	"rotate_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
}


def admin_bodies(client) -> list[str]:
	"""Every admin response body a token could leak into."""
	pages = ["/_admin/identities", "/_admin/api/identities", "/_admin/rules", "/_admin/cache", "/_admin/approvals", "/_admin/api/approvals"]
	return [client.get(path).text for path in pages]


# --- listing -------------------------------------------------------------------

def test_list_shows_health_and_never_a_token(client, add_identity):
	add_identity(token=SECRET, days_left=20)
	add_identity(name="tired", token="tok-tired", days_left=2)
	add_identity(name="gone", token="tok-gone", days_left=0)
	html = client.get("/_admin/identities").text
	assert "<h1>Identities</h1>" in html
	assert 'class="fill green"' in html and 'class="fill red"' in html and 'class="fill expired"' in html
	assert "jira-uat" in html and "ran@example.com" in html
	assert "manage-profile/security/api-tokens" in html
	for body in admin_bodies(client):
		assert SECRET not in body and "tok-tired" not in body and "tok-gone" not in body


def test_api_list(client, add_identity):
	identity = add_identity(token=SECRET, days_left=29)
	add_identity(name="notoken", token=None)
	r = client.get("/_admin/api/identities")
	rows = {row["name"]: row for row in r.json()["identities"]}
	mine = rows["jira-uat"]
	assert mine["upstreams"] == ["jira"] and mine["scheme"] == "basic" and mine["username"] == "ran@example.com"
	assert mine["health"]["level"] == "green" and 0.9 < mine["health"]["fraction"] <= 1.0
	assert mine["expires_at"] == identity.expires_at.isoformat(timespec="seconds")
	assert mine["last_status"] is None
	assert mine["has_token"] is True and rows["notoken"]["has_token"] is False
	assert mine["rotate_url"].startswith("https://id.atlassian.com/")
	assert "token" not in mine and SECRET not in r.text


def test_rejected_and_amber_levels(client, add_identity, fake_upstream):
	add_identity(token=SECRET, days_left=10)
	add_identity(name="warm", token="tok-w", days_left=20, lifetime=30)   # fraction ~0.67: green
	add_identity(name="amber", token="tok-a", days_left=10, lifetime=30)  # fraction ~0.33: amber
	fake_upstream.responder = lambda req: httpx.Response(401)
	client.get("/x", headers=ID)
	rows = {row["name"]: row["health"]["level"] for row in client.get("/_admin/api/identities").json()["identities"]}
	assert rows == {"jira-uat": "rejected", "warm": "green", "amber": "amber"}
	html = client.get("/_admin/identities").text
	assert 'class="fill rejected"' in html and 'class="fill amber"' in html


# --- add -------------------------------------------------------------------------

def test_add_via_page_creates_keychain_item_and_row(client, secrets, admin_db, fake_upstream):
	r = client.post("/_admin/identities", data=ADD, follow_redirects=False)
	assert r.status_code == 303
	assert secrets.get("jira-uat") == SECRET
	identity = db.get_identity(admin_db, "jira-uat")
	assert identity.upstream_ids == () and identity.username == "ran@example.com"
	assert timedelta(days=29) < identity.expires_at - datetime.now(timezone.utc) <= timedelta(days=30)
	assert client.get("/x", headers=ID).status_code == 403   # created first, bound later
	db.set_upstream_identities(admin_db, "jira", ["jira-uat"])
	assert client.get("/x", headers=ID).status_code == 200   # the running proxy knows it already
	assert fake_upstream.requests[-1].headers["authorization"] == "Basic " + base64.b64encode(f"ran@example.com:{SECRET}".encode()).decode()
	for body in admin_bodies(client):
		assert SECRET not in body


def test_add_via_api(client, secrets, admin_db):
	r = client.post("/_admin/api/identities", json={**ADD, "name": "svc", "scheme": "bearer", "username": None, "expires": "2027-01-01", "issued": "2026-10-01"})
	assert r.status_code == 201
	body = r.json()
	assert body["name"] == "svc" and body["scheme"] == "bearer" and "token" not in body
	assert body["expires_at"] == "2027-01-01T00:00:00+00:00" and body["issued_at"] == "2026-10-01T00:00:00+00:00"
	assert secrets.get("svc") == SECRET


def test_add_custom_via_api_stores_the_whole_header_value(client, secrets, admin_db):
	r = client.post("/_admin/api/identities", json={**ADD, "name": "proj", "scheme": "custom", "username": None, "token": "Token tok-secret-c1"})
	assert r.status_code == 201
	assert r.json()["scheme"] == "custom" and "Token tok-secret-c1" not in r.text
	assert secrets.get("proj") == "Token tok-secret-c1"


def test_add_custom_via_api_with_a_header_name(client, secrets, admin_db, fake_upstream):
	r = client.post("/_admin/api/identities", json={**ADD, "name": "claude", "scheme": "custom", "username": None, "header": "x-api-key", "token": "sk-secret-c2"})
	assert r.status_code == 201
	assert r.json()["header"] == "x-api-key" and "sk-secret-c2" not in r.text
	assert secrets.get("claude") == "sk-secret-c2"
	db.set_upstream_identities(admin_db, "jira", ["claude"])
	client.get("/x", headers={"X-WR-Identity": "claude"})
	assert fake_upstream.requests[-1].headers["x-api-key"] == "sk-secret-c2"
	for body in admin_bodies(client):
		assert "sk-secret-c2" not in body


def test_add_custom_with_a_blank_header_sends_authorization(client, admin_db):
	r = client.post("/_admin/api/identities", json={**ADD, "name": "proj", "scheme": "custom", "username": None, "header": "", "token": "Token t"})
	assert r.status_code == 201 and r.json()["header"] == "Authorization"
	assert db.get_identity(admin_db, "proj").header == "Authorization"
	assert client.get("/_admin/api/identities").json()["identities"][0]["header"] == "Authorization"


def test_header_on_a_bearer_identity_is_refused_and_the_form_is_kept_without_the_token(client, secrets, admin_db):
	r = client.post("/_admin/identities", data={**ADD, "name": "svc", "scheme": "bearer", "username": "", "header": "x-api-key", "token": "tok-secret-b9"})
	assert r.status_code == 400 and "a bearer identity takes no header name" in r.text
	assert 'value="svc"' in r.text and 'value="x-api-key"' in r.text and 'value="bearer" checked' in r.text
	assert "tok-secret-b9" not in r.text
	assert secrets.get("svc") is None and db.get_identity(admin_db, "svc") is None


@pytest.mark.parametrize("header", ["Host", "x-wr-identity", "bad name"])
def test_reserved_or_malformed_header_names_are_refused(client, secrets, header):
	r = client.post("/_admin/api/identities", json={**ADD, "name": "proj", "scheme": "custom", "username": None, "header": header, "token": "v"})
	assert r.status_code == 400 and "header name" in r.text
	assert secrets.get("proj") is None


def test_add_custom_via_page_refuses_a_header_break_without_echoing_it(client, secrets, admin_db):
	r = client.post("/_admin/identities", data={**ADD, "name": "proj", "scheme": "custom", "username": "", "token": "Token abc\r\nX-Evil: 1"})
	assert r.status_code == 400 and "header value" in r.text
	assert "X-Evil" not in r.text
	assert secrets.get("proj") is None and db.get_identity(admin_db, "proj") is None


def test_rotate_custom_via_api_keeps_inner_spaces(client, add_identity, secrets):
	add_identity(name="proj", scheme="custom", username=None, header="Authorization", token="Token old")
	r = client.post("/_admin/api/identities/proj/token", json={"token": "Token new-one", "expires": "30d"})
	assert r.status_code == 200
	assert secrets.get("proj") == "Token new-one"


def test_add_refused_leaves_nothing_and_never_echoes_the_token(client, secrets, admin_db):
	r = client.post("/_admin/identities", data={**ADD, "username": "a:b"})
	assert r.status_code == 400
	assert 'class="flash error"' in r.text and "username" in r.text
	assert 'value="jira-uat"' in r.text            # the typed fields come back
	assert SECRET not in r.text                     # the token never does
	assert secrets.get("jira-uat") is None
	assert db.get_identity(admin_db, "jira-uat") is None
	r = client.post("/_admin/api/identities", json={**ADD, "token": "has space"})
	assert r.status_code == 400 and "token" in r.json()["error"]
	r = client.post("/_admin/api/identities", json={**ADD, "expires": "soon"})
	assert r.status_code == 400 and "bad time" in r.json()["error"]


def test_add_with_a_failing_keychain_leaves_no_row(client, secrets, admin_db):
	def locked(name, token):
		raise KeychainError(f"Keychain write failed for '{name}': KeyringLocked: Keychain Access Denied")

	secrets.set = locked
	r = client.post("/_admin/identities", data=ADD)
	assert r.status_code == 500
	assert "KeyringLocked" in r.text and SECRET not in r.text
	assert db.get_identity(admin_db, "jira-uat") is None


# --- rotate --------------------------------------------------------------------------

def test_rotate_via_page_changes_the_next_injection_and_clears_rejected(client, add_identity, secrets, admin_db, fake_upstream):
	add_identity(token="tok-old")
	fake_upstream.responder = lambda req: httpx.Response(401)
	client.get("/x", headers=ID)
	assert db.get_identity(admin_db, "jira-uat").last_status == "rejected"
	fake_upstream.responder = lambda req: httpx.Response(200, json={})
	r = client.post("/_admin/identities/Jira-UAT/token", data={"token": SECRET, "expires": "2027-03-01"}, follow_redirects=False)
	assert r.status_code == 303
	assert secrets.get("jira-uat") == SECRET and secrets.get("Jira-UAT") is None
	after = db.get_identity(admin_db, "jira-uat")
	assert after.last_status is None and after.expires_at == datetime(2027, 3, 1, tzinfo=timezone.utc)
	client.get("/y", headers=ID)
	assert SECRET.encode() in base64.b64decode(fake_upstream.requests[-1].headers["authorization"].split()[1])
	for body in admin_bodies(client):
		assert SECRET not in body


def test_rotate_via_api_and_refusals(client, add_identity, secrets):
	add_identity(token="tok-old")
	r = client.post("/_admin/api/identities/jira-uat/token", json={"token": SECRET, "expires": "60d", "rotate_url": "https://new.example/rotate"})
	assert r.status_code == 200
	assert r.json()["rotate_url"] == "https://new.example/rotate" and "token" not in r.json()
	assert secrets.get("jira-uat") == SECRET
	assert client.post("/_admin/api/identities/ghost/token", json={"token": SECRET, "expires": "60d"}).status_code == 404
	r = client.post("/_admin/api/identities/jira-uat/token", json={"token": "", "expires": "60d"})
	assert r.status_code == 400
	assert secrets.get("jira-uat") == SECRET
	r = client.post("/_admin/identities/jira-uat/token", data={"token": "tok-x", "expires": "2020-01-01"})
	assert r.status_code == 400 and "expires_at must be after" in r.text and "tok-x" not in r.text
	assert secrets.get("jira-uat") == SECRET


# --- delete --------------------------------------------------------------------------

def test_delete_via_page_and_api(client, add_identity, secrets, admin_db):
	add_identity(token="tok-1")
	add_identity(name="other", token="tok-2")
	db.put_approval(admin_db, "192.0.2.111", db.get_identity(admin_db, "jira-uat").id)
	r = client.post("/_admin/identities/Jira-UAT/delete", follow_redirects=False)
	assert r.status_code == 303
	assert secrets.get("jira-uat") is None and db.get_identity(admin_db, "jira-uat") is None
	assert db.list_approvals(admin_db) == []
	assert client.delete("/_admin/api/identities/other").json() == {"deleted": True, "name": "other", "keychain_item": True}
	assert secrets.get("other") is None
	assert client.delete("/_admin/api/identities/other").status_code == 404
	assert client.get("/x", headers=ID).status_code == 400   # unknown identity now


def test_delete_stops_when_the_keychain_refuses(client, add_identity, secrets, admin_db):
	add_identity(token="tok-1")

	def locked_get(name):
		raise KeychainError(f"Keychain read failed for '{name}': KeyringLocked: Keychain Access Denied")

	secrets.delete, secrets.get = (lambda name: False), locked_get
	r = client.post("/_admin/identities/jira-uat/delete")
	assert r.status_code == 500 and "KeyringLocked" in r.text
	assert db.get_identity(admin_db, "jira-uat") is not None


def test_delete_stops_when_the_item_survives_the_delete(client, add_identity, secrets, admin_db):
	add_identity(token="tok-1")
	secrets.delete = lambda name: False
	r = client.delete("/_admin/api/identities/jira-uat")
	assert r.status_code == 500 and "nothing was deleted" in r.json()["error"]
	assert db.get_identity(admin_db, "jira-uat") is not None
	assert secrets.get("jira-uat") == "tok-1"


def test_delete_carries_on_when_the_item_is_truly_absent(client, add_identity, secrets, admin_db):
	add_identity(token=None)
	r = client.post("/_admin/identities/jira-uat/delete", follow_redirects=False)
	assert r.status_code == 303 and "no+Keychain+item" in r.headers["location"]
	assert db.get_identity(admin_db, "jira-uat") is None
	add_identity(name="again", token=None)
	assert client.delete("/_admin/api/identities/again").json() == {"deleted": True, "name": "again", "keychain_item": False}


# --- the add form ---------------------------------------------------------------------

def test_add_form_fields(client, add_identity):
	html = client.get("/_admin/identities").text
	assert 'type="password" name="token" autocomplete="off"' in html.replace("  ", " ")
	assert 'name="upstream_id"' not in html   # identities come first; upstreams are bound on the Upstreams page
	assert 'href="/_admin/upstreams"' in html
	assert 'name="scheme" value="basic"' in html and 'name="scheme" value="bearer"' in html
	assert 'name="scheme" value="custom"' in html and "the whole value of one header" in html
	assert 'name="header"' in html and 'placeholder="Authorization"' in html


def test_add_form_marks_the_fields_each_scheme_uses(client):
	"""admin.js shows a [data-scheme] element only while its scheme's radio is checked."""
	html = client.get("/_admin/identities").text
	assert '<label data-scheme="basic">Username <input type="text" name="username"' in html
	assert '<label data-scheme="custom">Header name <input type="text" name="header"' in html
	assert '<p class="hint" data-scheme="custom">' in html
	assert "(basic only)" not in html and "(custom only)" not in html
	assert html.count("data-scheme=") == 3   # the token, dates and rotate URL belong to every scheme


def test_each_row_offers_its_x_wr_identity_line_to_copy(client, add_identity):
	"""The whole header line sits in data-copy for admin.js; nothing of it is in script text."""
	add_identity()
	add_identity(name="svc", token="tok-s")
	html = client.get("/_admin/identities").text
	assert '<code>X-WR-Identity: jira-uat</code> <button type="button" class="copy" data-copy="X-WR-Identity: jira-uat">copy</button>' in html
	assert html.count('class="copy"') == 2
	assert "writeText(" not in html


def test_list_shows_each_identitys_upstreams(client, admin_db, add_identity):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	add_identity(upstream_id=["jira", "conf"])
	add_identity(name="loose", token="tok-l", upstream_id=None)
	html = client.get("/_admin/identities").text
	assert "<code>conf</code><br><code>jira</code>" in html
	assert "none — bind it on" in html
	rows = {row["name"]: row["upstreams"] for row in client.get("/_admin/api/identities").json()["identities"]}
	assert rows == {"jira-uat": ["conf", "jira"], "loose": []}


def test_rotate_field_asks_a_custom_identity_for_its_header_value(client, add_identity):
	add_identity(name="proj", scheme="custom", username=None, header="Authorization", token="Token old")
	add_identity(name="claude", scheme="custom", username=None, header="x-api-key", token="sk-1")
	add_identity()
	html = client.get("/_admin/identities").text
	assert html.count('placeholder="new Authorization value, e.g. Token abc123"') == 1
	assert html.count('placeholder="new x-api-key value"') == 1
	assert html.count('placeholder="new token"') == 1
	assert 'custom<br><span class="hint">x-api-key</span>' in html   # the header name shows where basic shows its username
