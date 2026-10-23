# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import gzip

import httpx
import pytest

from proxy import db


def test_unknown_upstream_is_400_naming_the_configured_ones(client, fake_upstream):
	r = client.get("/x", headers={"X-WR-Upstream": "nope"})
	assert r.status_code == 400
	assert "'nope'" in r.text and "jira" in r.text
	assert fake_upstream.requests == []


def test_root_path_is_forwarded(client, fake_upstream):
	assert client.get("/").status_code == 200
	assert str(fake_upstream.requests[0].url) == "https://jira.test/"


def test_reserved_underscore_prefix_is_never_proxied(client, fake_upstream):
	assert client.get("/_internal").status_code == 404
	assert client.get("/_internal/rules").status_code == 404
	assert fake_upstream.requests == []


def test_get_without_rule_passes_through(client, fake_upstream):
	r = client.get("/rest/api/2/issue/ABC-1?fields=summary")
	assert r.status_code == 200
	assert r.json() == {"ok": True}
	assert r.headers["x-cache"] == "PASS"
	assert str(fake_upstream.requests[0].url) == "https://jira.test/rest/api/2/issue/ABC-1?fields=summary"


def test_post_forwards_body_and_status(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(201, json={"echo": req.content.decode()})
	r = client.post("/rest/api/2/issue", content=b'{"a":1}', headers={"Content-Type": "application/json"})
	assert r.status_code == 201
	assert r.json() == {"echo": '{"a":1}'}
	assert r.headers["x-cache"] == "PASS"
	assert fake_upstream.requests[0].method == "POST"


def test_request_header_hygiene(client, fake_upstream):
	client.get("/x", headers={
		"Connection": "keep-alive, X-Hop",
		"X-Hop": "1",
		"Accept-Encoding": "x-unsupported",
	})
	sent = fake_upstream.requests[0].headers
	assert sent["host"] == "jira.test"
	assert "x-hop" not in sent
	assert "x-unsupported" not in sent.get("accept-encoding", "")


def test_percent_encoded_path_forwarded_byte_for_byte(client, fake_upstream):
	client.get("/rest/api/2/issue/a%2Fb%20c")
	assert fake_upstream.requests[0].url.raw_path == b"/rest/api/2/issue/a%2Fb%20c"


def test_framework_route_names_belong_to_upstreams(client, admin_db, fake_upstream):
	db.put_upstream(admin_db, db.Upstream("docs", "https://docs.test"))
	r = client.get("/docs", headers={"X-WR-Upstream": "docs"})
	assert r.headers["x-cache"] == "PASS"
	assert str(fake_upstream.requests[0].url) == "https://docs.test/docs"
	assert client.get("/openapi.json").headers["x-cache"] == "PASS"
	assert str(fake_upstream.requests[1].url) == "https://jira.test/openapi.json"


def test_gzip_upstream_is_served_decoded(client, fake_upstream):
	payload = b'{"ok":true}'
	fake_upstream.responder = lambda req: httpx.Response(
		200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
	)
	r = client.get("/x")
	assert r.content == payload
	assert "content-encoding" not in r.headers
	assert r.headers["content-length"] == str(len(payload))


def test_set_cookie_never_reaches_client(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(200, headers=[("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
	assert "set-cookie" not in client.get("/x").headers


def test_cookies_travel_neither_way(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(200, headers={"Set-Cookie": "JSESSIONID=abc"})
	response = client.post("/x", headers={"Cookie": "JSESSIONID=old"})
	assert "set-cookie" not in response.headers
	assert "cookie" not in fake_upstream.requests[-1].headers


def test_upstream_timeout_is_504(client, fake_upstream):
	def slow(req):
		raise httpx.ConnectTimeout("slow", request=req)
	fake_upstream.responder = slow
	r = client.get("/x")
	assert r.status_code == 504
	assert "jira" in r.text


def test_upstream_connect_error_is_502(client, fake_upstream):
	def refused(req):
		raise httpx.ConnectError("refused", request=req)
	fake_upstream.responder = refused
	assert client.get("/x").status_code == 502


def test_env_seed_creates_upstreams(client, admin_db):
	assert [u.upstream_id for u in db.list_upstreams(admin_db)] == ["jira"]


def test_upstream_x_cache_header_does_not_leak_on_pass(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(200, headers={"X-Cache": "Hit from cloudfront"})
	assert client.get("/x").headers.get_list("x-cache") == ["PASS"]


def test_proxy_generated_response_has_exactly_one_date_header(client):
	r = client.get("/x", headers={"X-WR-Upstream": "nope"})
	assert len(r.headers.get_list("date")) == 1


class _UnnormalizedURL(httpx.URL):
	"""httpx collapses dot segments in URLs; this one reports the path exactly as given."""

	def __init__(self, url, raw_path: bytes):
		super().__init__(url)
		self._raw = raw_path

	@property
	def raw_path(self) -> bytes:
		return self._raw


def test_head_keeps_upstream_content_length(client, fake_upstream):
	fake_upstream.responder = lambda request: httpx.Response(200, headers={"Content-Length": "1234"})
	r = client.head("/x")
	assert r.headers["content-length"] == "1234"
	assert len(r.headers.get_list("content-length")) == 1


@pytest.mark.parametrize("raw", ["/../x", "/./x", "/%2e%2e/x", "/.%2E/x"])
def test_dot_segments_are_rejected(client, fake_upstream, raw):
	request = client.build_request("GET", "http://testserver/")
	request.url = _UnnormalizedURL("http://testserver/", raw.encode())
	r = client.send(request)
	assert r.status_code == 400
	assert fake_upstream.requests == []
