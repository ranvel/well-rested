# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import base64
import time

import httpx
import pytest

from proxy import db
from proxy.approvals import Decision

ID = {"X-WR-Identity": "jira-uat"}


@pytest.fixture
def cached(admin_db):
	db.create_rule(admin_db, "jira", "/**", 3600)


def upstream_auth(fake_upstream) -> str | None:
	return fake_upstream.requests[-1].headers.get("authorization")


def test_basic_identity_is_injected(client, add_identity, fake_upstream):
	add_identity()
	assert client.get("/x", headers=ID).status_code == 200
	assert upstream_auth(fake_upstream) == "Basic " + base64.b64encode(b"ran@example.com:tok-1").decode()
	assert "x-wr-identity" not in fake_upstream.requests[-1].headers


def test_bearer_identity_is_injected(client, add_identity, fake_upstream):
	add_identity(name="svc", scheme="bearer", username=None, token="tok-b")
	client.post("/x", headers={"X-WR-Identity": "svc"})
	assert upstream_auth(fake_upstream) == "Bearer tok-b"


def test_custom_identity_sends_its_header_value_verbatim(client, add_identity, fake_upstream):
	add_identity(name="proj", scheme="custom", username=None, header="Authorization", token="Token abc123")
	client.get("/x", headers={"X-WR-Identity": "proj"})
	assert upstream_auth(fake_upstream) == "Token abc123"


def test_custom_identity_sends_its_value_under_its_own_header(client, add_identity, fake_upstream):
	add_identity(name="claude", scheme="custom", username=None, header="x-api-key", token="sk-1")
	assert client.get("/x", headers={"X-WR-Identity": "claude"}).status_code == 200
	sent = fake_upstream.requests[-1].headers
	assert sent.get_list("x-api-key") == ["sk-1"]
	assert "authorization" not in sent


def test_a_client_header_named_like_the_injected_one_is_replaced_not_joined(client, add_identity, fake_upstream):
	add_identity(name="claude", scheme="custom", username=None, header="x-api-key", token="sk-1")
	client.get("/x", headers={"X-WR-Identity": "claude", "X-API-Key": "mine", "X-Other": "kept"})
	sent = fake_upstream.requests[-1].headers
	assert sent.get_list("x-api-key") == ["sk-1"]
	assert sent["x-other"] == "kept"


def test_a_cookie_identity_replaces_the_client_cookie(client, add_identity, fake_upstream):
	add_identity(name="site", scheme="custom", username=None, header="Cookie", token="session=abc; theme=dark")
	client.get("/x", headers={"X-WR-Identity": "site", "Cookie": "session=mine"})
	sent = fake_upstream.requests[-1].headers
	assert sent.get_list("cookie") == ["session=abc; theme=dark"]
	assert "authorization" not in sent


def test_custom_identity_is_marked_rejected_on_401(client, admin_db, add_identity, fake_upstream):
	add_identity(name="proj", scheme="custom", username=None, header="Authorization", token="Token abc123")
	fake_upstream.responder = lambda req: httpx.Response(401)
	r = client.get("/x", headers={"X-WR-Identity": "proj"})
	assert r.status_code == 401 and "was rejected by the upstream (401)" in r.headers["warning"]
	assert db.get_identity(admin_db, "proj").last_status == "rejected"


def test_anonymous_gets_no_credentials(client, fake_upstream):
	client.get("/x")
	assert upstream_auth(fake_upstream) is None


@pytest.mark.parametrize("headers, fragment", [
	({"Authorization": "Basic abc"}, "X-WR-Identity"),
	({"X-WR-Identity": ""}, "empty"),
	({"X-WR-Identity": "ghost"}, "unknown identity"),
])
def test_bad_identity_requests_never_reach_the_upstream(client, add_identity, fake_upstream, headers, fragment):
	add_identity()
	response = client.get("/x", headers=headers)
	assert response.status_code == 400
	assert fragment in response.text
	assert fake_upstream.requests == []


def test_repeated_identity_header_is_refused(client, add_identity, fake_upstream):
	add_identity()
	response = client.get("/x", headers=[("X-WR-Identity", "jira-uat"), ("X-WR-Identity", "jira-uat")])
	assert response.status_code == 400
	assert fake_upstream.requests == []


def test_identity_is_refused_on_an_upstream_it_is_not_bound_to(client, admin_db, add_identity, fake_upstream):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	add_identity()
	response = client.get("/x", headers={**ID, "X-WR-Upstream": "conf"})
	assert response.status_code == 403
	assert "not bound to upstream 'conf'; bound to: jira" in response.text
	assert fake_upstream.requests == []


def test_identity_bound_to_nothing_is_refused(client, add_identity, fake_upstream):
	add_identity(upstream_id=None)
	response = client.get("/x", headers=ID)
	assert response.status_code == 403
	assert "not bound to upstream 'jira'; bound to: nothing" in response.text
	assert fake_upstream.requests == []


def test_one_identity_serves_every_upstream_it_is_bound_to(client, cached, admin_db, add_identity, fake_upstream):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))
	db.create_rule(admin_db, "conf", "/**", 3600)
	add_identity(upstream_id=["jira", "conf"])
	expected = "Basic " + base64.b64encode(b"ran@example.com:tok-1").decode()
	for upstream_id in ("jira", "conf"):
		assert client.get("/x", headers={**ID, "X-WR-Upstream": upstream_id}).headers["x-cache"] == "MISS"
		assert upstream_auth(fake_upstream) == expected
	assert {str(r.url) for r in fake_upstream.requests} == {"https://jira.test/x", "https://conf.test/x"}
	for upstream_id in ("jira", "conf"):   # each upstream keeps its own entry for the same path
		assert client.get("/x", headers={**ID, "X-WR-Upstream": upstream_id}).headers["x-cache"] == "HIT"


def test_unbinding_takes_effect_on_the_next_request(client, admin_db, add_identity, fake_upstream):
	add_identity()
	assert client.get("/x", headers=ID).status_code == 200
	db.set_upstream_identities(admin_db, "jira", [])
	assert client.get("/x", headers=ID).status_code == 403
	assert len(fake_upstream.requests) == 1


def test_missing_keychain_token_is_503(client, add_identity, fake_upstream):
	add_identity(token=None)
	response = client.get("/x", headers=ID)
	assert response.status_code == 503
	assert "identity rotate jira-uat" in response.text
	assert fake_upstream.requests == []


def test_identities_partition_the_cache(client, cached, add_identity):
	add_identity()
	add_identity(name="other", token="tok-2")
	assert client.get("/me", headers=ID).headers["x-cache"] == "MISS"
	assert client.get("/me", headers={"X-WR-Identity": "other"}).headers["x-cache"] == "MISS"
	assert client.get("/me").headers["x-cache"] == "MISS"
	assert client.get("/me", headers=ID).headers["x-cache"] == "HIT"


def test_identity_never_reads_an_anonymous_entry(client, cached, add_identity, fake_upstream):
	add_identity()
	assert client.get("/me").headers["x-cache"] == "MISS"   # anonymous warms the path first
	assert client.get("/me").headers["x-cache"] == "HIT"
	assert client.get("/me", headers=ID).headers["x-cache"] == "MISS"
	assert len(fake_upstream.requests) == 2


def test_rotation_keeps_the_cache_and_uses_the_new_token(client, cached, admin_db, add_identity, secrets, fake_upstream):
	identity = add_identity()
	client.get("/me", headers=ID)
	secrets.set("jira-uat", "tok-new")
	db.rotate_identity(admin_db, "jira-uat", issued_at=identity.issued_at, expires_at=identity.expires_at)
	assert client.get("/me", headers=ID).headers["x-cache"] == "HIT"
	client.get("/fresh", headers=ID)
	assert upstream_auth(fake_upstream) == "Basic " + base64.b64encode(b"ran@example.com:tok-new").decode()


def test_recreated_identity_cannot_read_the_old_cache(client, cached, admin_db, add_identity):
	add_identity()
	client.get("/me", headers=ID)
	db.delete_identity(admin_db, "jira-uat")
	add_identity(token="tok-someone-else")
	assert client.get("/me", headers=ID).headers["x-cache"] == "MISS"


def test_token_is_never_persisted_or_logged(make_client, add_identity, settings, capsys):
	client = make_client(log_level="DEBUG")
	conn = db.connect(settings.db_path)
	db.create_rule(conn, "jira", "/**", 3600)
	conn.close()
	add_identity(token="tok-secret-7f3a9c")
	client.get("/me", headers=ID)
	client.post("/me", headers=ID)
	client.get("/nope", headers={"X-WR-Identity": "ghost"})
	needle = b"tok-secret-7f3a9c"
	files = [settings.db_path, *settings.db_path.parent.glob("proxy.db*"), *settings.cache_dir.iterdir()]
	assert all(needle not in path.read_bytes() for path in files if path.is_file())
	assert needle.decode() not in capsys.readouterr().err


def test_lan_client_is_asked_once(make_client, add_identity, prompter):
	lan = make_client(client_addr="192.0.2.111")
	add_identity()
	assert lan.get("/x", headers=ID).status_code == 200
	assert lan.get("/y", headers=ID).status_code == 200
	assert prompter.messages == [
		'192.0.2.111 is trying to connect to https://jira.test using the "jira-uat" identity. Allow?'
	]


def test_denied_lan_client_cannot_read_a_warm_cache(client, cached, add_identity, prompter, fake_upstream):
	add_identity()
	client.get("/me", headers=ID)   # warm the cache from loopback
	client.app.addr = "192.0.2.111"     # same app, now seen from the LAN (ClientAddr shim)
	prompter.answer = Decision.NO
	response = client.get("/me", headers=ID)
	assert response.status_code == 403
	assert "x-cache" not in response.headers
	assert len(fake_upstream.requests) == 1


def test_anonymous_lan_client_is_not_asked(make_client, prompter):
	assert make_client(client_addr="192.0.2.111").get("/x").status_code == 200
	assert prompter.messages == []


def test_pending_approval_asks_the_client_to_retry(make_client, add_identity, prompter):
	lan = make_client(client_addr="192.0.2.111", approval_wait_s=0.05)
	add_identity()
	prompter.hold = True
	response = lan.get("/x", headers=ID)
	assert response.status_code == 503
	assert response.headers["retry-after"] == "5"
	prompter.release()
	time.sleep(0.2)
	assert lan.get("/x", headers=ID).status_code == 200


def test_401_marks_the_token_rejected_and_warns(client, admin_db, add_identity, fake_upstream):
	add_identity()
	fake_upstream.responder = lambda req: httpx.Response(401)
	response = client.get("/x", headers=ID)
	assert "was rejected by the upstream (401)" in response.headers["warning"]
	assert db.get_identity(admin_db, "jira-uat").last_status == "rejected"
	fake_upstream.responder = lambda req: httpx.Response(200, json={})
	response = client.get("/x", headers=ID)
	assert "warning" not in response.headers
	assert db.get_identity(admin_db, "jira-uat").last_status == "ok"


def test_red_token_warns_on_hits_and_misses(client, cached, add_identity):
	add_identity(days_left=2)
	miss = client.get("/me", headers=ID)
	hit = client.get("/me", headers=ID)
	assert hit.headers["x-cache"] == "HIT"
	for response in (miss, hit):
		assert response.headers["warning"].startswith("299 - \"token for 'jira-uat' expires ")
		assert "rotate at https://id.atlassian.com/" in response.headers["warning"]


def test_green_token_does_not_warn(client, cached, add_identity):
	add_identity(days_left=29)
	assert "warning" not in client.get("/me", headers=ID).headers


def test_failed_keychain_read_is_503_naming_the_error_and_recovers(make_client, secrets, add_identity, fake_upstream):
	client = make_client()   # the app seeds the jira upstream; identities are loaded on the next request
	identity = add_identity()
	add_identity(name="fine", token="tok-fine")
	real_get = secrets.get

	def locked_get(name):
		if name == "jira-uat" and locked:
			raise RuntimeError("Keychain Access Denied")
		return real_get(name)

	locked = True
	secrets.get = locked_get
	response = client.get("/x", headers=ID)
	assert response.status_code == 503
	assert "Keychain read" in response.text and "RuntimeError" in response.text
	assert "identity rotate" not in response.text
	assert fake_upstream.requests == []
	assert client.get("/x", headers={"X-WR-Identity": "fine"}).status_code == 200
	locked = False
	conn = db.connect(client.app.state.settings.db_path)
	db.rotate_identity(conn, "jira-uat", issued_at=identity.issued_at, expires_at=identity.expires_at)
	conn.close()
	assert client.get("/x", headers=ID).status_code == 200
