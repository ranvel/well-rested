# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The Approvals admin page: answer pending prompts, revoke grants and Always rows, clear cooldowns."""

import time

import pytest

from proxy import db

ID = {"X-WR-Identity": "jira-uat"}
LAN = "192.0.2.111"


@pytest.fixture
def lan(make_client, add_identity, prompter):
	"""One app seen from the LAN (requests park on the held prompt) and, via .addr, from loopback."""
	client = make_client(client_addr=LAN, approval_wait_s=0.05, approval_prompt_timeout_s=1.0)
	add_identity()
	prompter.hold = True
	yield client
	prompter.release()


def from_loopback(client):
	client.app.addr = "127.0.0.1"
	return client


def from_lan(client):
	client.app.addr = LAN
	return client


def park(client):
	r = from_lan(client).get("/x", headers=ID)
	assert r.status_code == 503 and r.headers["retry-after"] == "5"
	return r


def test_pending_appears_and_is_answered_on_the_page(lan, admin_db, fake_upstream):
	park(lan)
	identity_id = db.get_identity(admin_db, "jira-uat").id
	body = from_loopback(lan).get("/_admin/api/approvals").json()
	assert body["pending"] == [{
		"client_addr": LAN, "identity_id": identity_id, "identity": "jira-uat",
		"message": body["pending"][0]["message"], "asked_at": body["pending"][0]["asked_at"],
	}]
	assert "jira-uat" in body["pending"][0]["message"] and body["grants"] == [] and body["cooldowns"] == []
	html = lan.get("/_admin/approvals").text
	assert "<h1>Approvals</h1>" in html and LAN in html and 'id="pending"' in html
	for label in ("Always", "One hour", "Session", "No"):
		assert f'value="{label}"' in html
	r = lan.post("/_admin/approvals/answer", data={"client_addr": LAN, "identity_id": str(identity_id), "decision": "One hour"})
	assert r.status_code == 200
	assert r.text.lstrip().startswith('<div id="pending"')
	assert LAN not in r.text
	time.sleep(0.05)
	assert from_lan(lan).get("/x", headers=ID).status_code == 200
	assert len(fake_upstream.requests) == 1
	grants = from_loopback(lan).get("/_admin/api/approvals").json()["grants"]
	assert grants == [{"client_addr": LAN, "identity_id": identity_id, "identity": "jira-uat", "kind": "hour", "expires_in_s": grants[0]["expires_in_s"]}]
	assert 3590 <= grants[0]["expires_in_s"] <= 3600


def test_answer_no_denies_and_lists_a_cooldown(lan, admin_db):
	park(lan)
	identity_id = db.get_identity(admin_db, "jira-uat").id
	r = from_loopback(lan).post("/_admin/api/approvals/answer", json={"client_addr": LAN, "identity_id": identity_id, "decision": "No"})
	assert r.status_code == 200 and r.json() == {"answered": True}
	time.sleep(0.05)
	assert from_lan(lan).get("/x", headers=ID).status_code == 403
	body = from_loopback(lan).get("/_admin/api/approvals").json()
	assert body["cooldowns"][0]["identity"] == "jira-uat" and 290 <= body["cooldowns"][0]["remaining_s"] <= 300
	r = lan.post("/_admin/api/approvals/cooldowns/clear", json={"client_addr": LAN, "identity_id": identity_id})
	assert r.json() == {"cleared": True}
	assert from_loopback(lan).get("/_admin/api/approvals").json()["cooldowns"] == []
	park(lan)   # asked again


def test_answer_without_pending_is_404(client, add_identity, admin_db):
	identity_id = add_identity().id
	r = client.post("/_admin/api/approvals/answer", json={"client_addr": LAN, "identity_id": identity_id, "decision": "Session"})
	assert r.status_code == 404
	r = client.post("/_admin/approvals/answer", data={"client_addr": LAN, "identity_id": str(identity_id), "decision": "Session"})
	assert r.status_code == 404
	assert r.text.lstrip().startswith('<div id="pending"') and "nothing is pending" in r.text   # the partial, so htmx shows it
	r = client.post("/_admin/api/approvals/answer", json={"client_addr": LAN, "identity_id": identity_id, "decision": "Maybe"})
	assert r.status_code == 400 and "decision" in r.json()["error"]


def test_always_rows_and_revoke(lan, admin_db, prompter):
	identity = db.get_identity(admin_db, "jira-uat")
	db.put_approval(admin_db, LAN, identity.id)
	assert from_lan(lan).get("/x", headers=ID).status_code == 200   # no prompt at all
	body = from_loopback(lan).get("/_admin/api/approvals").json()
	assert body["always"][0]["client_addr"] == LAN and body["always"][0]["identity"] == "jira-uat"
	html = lan.get("/_admin/approvals").text
	assert "<h2>Always</h2>" in html and LAN in html
	r = lan.post("/_admin/approvals/always/revoke", data={"client_addr": LAN, "identity": "jira-uat"}, follow_redirects=False)
	assert r.status_code == 303
	assert db.list_approvals(admin_db) == []
	park(lan)   # asked again, immediately
	assert prompter.messages[-1].startswith(LAN)
	assert from_loopback(lan).post("/_admin/api/approvals/always/revoke", json={"client_addr": LAN, "identity": "jira-uat"}).status_code == 404


def test_revoke_session_grant(lan, admin_db, prompter):
	prompter.hold = False
	prompter.answer = __import__("proxy.approvals", fromlist=["Decision"]).Decision.SESSION
	assert from_lan(lan).get("/x", headers=ID).status_code == 200
	identity_id = db.get_identity(admin_db, "jira-uat").id
	body = from_loopback(lan).get("/_admin/api/approvals").json()
	assert body["grants"] == [{"client_addr": LAN, "identity_id": identity_id, "identity": "jira-uat", "kind": "session", "expires_in_s": None}]
	r = lan.post("/_admin/approvals/grants/revoke", data={"client_addr": LAN, "identity_id": str(identity_id)}, follow_redirects=False)
	assert r.status_code == 303
	assert from_loopback(lan).get("/_admin/api/approvals").json()["grants"] == []
	assert lan.post("/_admin/api/approvals/grants/revoke", json={"client_addr": LAN, "identity_id": identity_id}).status_code == 404
	prompter.hold = True
	park(lan)   # asked again


def test_pending_partial_polls(lan, admin_db):
	empty = from_loopback(lan).get("/_admin/approvals/pending")
	assert empty.status_code == 200
	assert 'hx-get="/_admin/approvals/pending"' in empty.text and 'hx-trigger="every 2s"' in empty.text
	assert "Nothing pending" in empty.text
	park(lan)
	live = from_loopback(lan).get("/_admin/approvals/pending").text
	assert LAN in live and "jira-uat" in live and 'name="decision"' in live


def test_deleted_identity_renders_by_id(lan, admin_db, prompter, secrets):
	from proxy.approvals import Decision
	prompter.hold = False
	prompter.answer = Decision.SESSION
	assert from_lan(lan).get("/x", headers=ID).status_code == 200
	identity_id = db.get_identity(admin_db, "jira-uat").id
	secrets.delete("jira-uat")
	db.delete_identity(admin_db, "jira-uat")
	body = from_loopback(lan).get("/_admin/api/approvals").json()
	assert body["grants"][0]["identity"] == f"deleted identity #{identity_id}"
	assert f"deleted identity #{identity_id}" in lan.get("/_admin/approvals").text
