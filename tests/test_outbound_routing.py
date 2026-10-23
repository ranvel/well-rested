# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbound proxies through the whole app: route resolution, the cache, and (Task 5) error mapping."""

import httpx

from proxy import db
from tests.test_caching import shift_clock

ROUTED = "outbound proxy 'corp-egress' (http://10.0.0.5:3128)"


def test_app_loads_proxy_passwords(client, add_proxy):
	add_proxy()
	client.get("/x")
	route = client.app.state.proxies.route("corp-egress")
	assert route.password == "pw-1"


def test_cached_entry_is_still_served_after_the_route_changes(client, admin_db, add_proxy, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	assert client.get("/x").headers["x-cache"] == "MISS"
	add_proxy()                                                     # jira now routes through corp-egress
	assert client.get("/x").headers["x-cache"] == "HIT"
	up = db.get_upstream(admin_db, "jira")
	db.put_upstream(admin_db, db.Upstream(up.upstream_id, up.base_url, up.verify_tls, up.timeout_s, proxy=None))
	assert client.get("/x").headers["x-cache"] == "HIT"
	assert len(fake_upstream.requests) == 1


def tunnel_refused(request):
	raise httpx.ProxyError("407 Proxy Authentication Required", request=request)


def test_tunnel_refusal_is_502_naming_the_proxy(client, add_proxy, fake_upstream):
	add_proxy()
	fake_upstream.responder = tunnel_refused
	r = client.get("/x")
	assert r.status_code == 502
	assert ROUTED in r.text and "407" in r.text


def test_forwarded_407_is_502_but_only_when_proxied(client, admin_db, add_proxy, fake_upstream):
	db.put_upstream(admin_db, db.Upstream("jira", "http://jira.test"))
	fake_upstream.responder = lambda req: httpx.Response(407)
	assert client.get("/x").status_code == 407                   # Direct: the origin's own 407
	add_proxy()
	r = client.get("/x")
	assert r.status_code == 502 and "rejected credentials" in r.text


def test_missing_password_is_503_and_never_dials_direct(client, add_proxy, fake_upstream):
	add_proxy(password=None)
	r = client.get("/x")
	assert r.status_code == 503
	assert "outbound proxy 'corp-egress'" in r.text and "no password in the Keychain" in r.text
	assert fake_upstream.requests == []


def connect_refused(request):
	raise httpx.ConnectError("refused", request=request)


def test_unreachable_proxy_is_502(client, add_proxy, fake_upstream):
	add_proxy()
	fake_upstream.responder = connect_refused
	r = client.get("/x")
	assert r.status_code == 502 and "connecting via " + ROUTED in r.text


def test_proxy_failures_serve_stale(client, admin_db, add_proxy, fake_upstream, proxy_secrets, monkeypatch):
	db.create_rule(admin_db, "jira", "/**", 60)
	add_proxy()
	fake_upstream.responder = lambda req: httpx.Response(200, json={"from": "warm"})
	assert client.get("/x").headers["x-cache"] == "MISS"
	shift_clock(monkeypatch, 61)
	fake_upstream.responder = tunnel_refused
	r = client.get("/x")
	assert (r.status_code, r.headers["x-cache"], r.json()) == (200, "STALE", {"from": "warm"})
	proxy_secrets.delete("corp-egress")
	db.update_proxy(admin_db, db.get_proxy(admin_db, "corp-egress"))   # bump: the registry reloads without it
	r = client.get("/x")
	assert (r.status_code, r.headers["x-cache"]) == (200, "STALE")


def test_password_never_reaches_logs_or_bodies(make_client, add_proxy, fake_upstream, capsys):
	client = make_client(log_level="DEBUG")
	add_proxy(password="pw-secret-9")
	fake_upstream.responder = tunnel_refused
	r = client.get("/x")
	assert "pw-secret-9" not in r.text
	assert "pw-secret-9" not in capsys.readouterr().err
