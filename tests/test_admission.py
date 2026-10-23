# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

from proxy.admission import HostGuard, is_loopback, normalize_host, parse_allowed_hosts


@pytest.mark.parametrize("value, expected", [
	("localhost", "localhost"),
	("LOCALHOST:8081", "localhost"),
	("mac.local.", "mac.local"),
	("[::1]:8081", "[::1]"),
	("[::1]", "[::1]"),
	("127.0.0.1:80", "127.0.0.1"),
	("", None),
	("host:port", None),
	("::1", None),          # IPv6 must be bracketed
	("[::1", None),
	("[::1]x", None),
	("localhost:\xb2", None),   # a Latin-1 superscript digit is not a port
	("[::1]:\xb3", None),
])
def test_normalize_host(value, expected):
	assert normalize_host(value) == expected


def test_parse_allowed_hosts():
	assert parse_allowed_hosts(" localhost , [::1],Mac.local ") == frozenset({"localhost", "[::1]", "mac.local"})
	with pytest.raises(ValueError):
		parse_allowed_hosts("localhost,host:port")
	with pytest.raises(ValueError):
		parse_allowed_hosts(" , ")


@pytest.mark.parametrize("addr, expected", [
	("127.0.0.1", True), ("127.8.8.8", True), ("::1", True), ("::ffff:127.0.0.1", True),
	("192.0.2.111", False), ("fe80::1", False), ("testclient", False), (None, False), ("", False),
])
def test_is_loopback(addr, expected):
	assert is_loopback(addr) is expected


def test_foreign_host_is_rejected_before_routing(client, fake_upstream):
	response = client.get("/x", headers={"Host": "evil.example"})
	assert response.status_code == 400
	assert fake_upstream.requests == []


def test_host_with_port_and_case_is_accepted(client):
	assert client.get("/x", headers={"Host": "LOCALHOST:8081"}).status_code == 200


def test_reserved_paths_are_loopback_only(make_client):
	lan = make_client(client_addr="192.0.2.111")
	assert lan.get("/_admin").status_code == 403
	assert lan.get("/_admin/api/rules", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
	assert lan.get("/_anything").status_code == 403


def test_reserved_paths_from_loopback_reach_the_router(client):
	assert client.get("/_admin", follow_redirects=False).status_code == 303   # the admin UI
	assert client.get("/_internal").status_code == 404                         # reserved, never proxied


async def test_missing_host_is_rejected():
	sent = []

	async def inner(scope, receive, send):
		raise AssertionError("the inner app must not be reached")

	async def send(message):
		sent.append(message)

	guard = HostGuard(inner, frozenset({"localhost"}))
	await guard({"type": "http", "path": "/x", "headers": [], "client": ("127.0.0.1", 1)}, None, send)
	assert sent[0]["status"] == 400


def test_lan_bind_with_loopback_only_allowlist_warns(make_client, capsys):
	make_client(host="0.0.0.0", allowed_hosts="localhost,127.0.0.1,[::1]")
	assert "PROXY_ALLOWED_HOSTS" in capsys.readouterr().err
