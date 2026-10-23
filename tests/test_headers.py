# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

from proxy.headers import (
	cacheable_response_headers,
	client_response_headers,
	decode_raw_headers,
	rewrite_location,
	upstream_request_headers,
)


def test_request_drops_hop_by_hop_and_proxy_managed_headers():
	out = upstream_request_headers([
		("Host", "proxy.local"),
		("Connection", "keep-alive, X-Hop"),
		("X-Hop", "1"),
		("Keep-Alive", "timeout=5"),
		("Content-Length", "3"),
		("Accept-Encoding", "br"),
		("Authorization", "Basic abc"),
		("Accept", "application/json"),
	])
	assert out == [("Accept", "application/json")]


def test_identity_and_cookies_never_go_upstream():
	out = upstream_request_headers([
		("X-WR-Identity", "jira-uat"), ("Cookie", "JSESSIONID=abc"),
		("Authorization", "Basic abc"), ("Accept", "application/json"),
	])
	assert out == [("Accept", "application/json")]


def test_client_response_drops_encoding_length_and_cookies():
	out = client_response_headers([
		("Content-Encoding", "gzip"),
		("Content-Length", "42"),
		("Transfer-Encoding", "chunked"),
		("Set-Cookie", "a=1"),
		("Set-Cookie", "b=2"),
		("Content-Type", "application/json"),
	])
	assert out == [("Content-Type", "application/json")]


def test_cacheable_headers_never_include_set_cookie():
	out = cacheable_response_headers([
		("set-cookie", "a=1"),
		("Content-Encoding", "gzip"),
		("Content-Type", "application/json"),
		("ETag", '"v1"'),
	])
	assert out == [("Content-Type", "application/json"), ("ETag", '"v1"')]


def test_decode_raw_headers_is_lossless_latin1():
	raw = [(b"Set-Cookie", b"a=1"), (b"Set-Cookie", b"b=2"), (b"X-Name", "café".encode("latin-1"))]
	assert decode_raw_headers(raw) == [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-Name", "café")]


def test_upstream_x_cache_and_age_do_not_reach_cached_entries():
	incoming = [("X-Cache", "Miss from cloudfront"), ("Age", "30"), ("Content-Type", "application/json")]
	assert client_response_headers(incoming) == [("Age", "30"), ("Content-Type", "application/json")]
	assert cacheable_response_headers(incoming) == [("Content-Type", "application/json")]


PROXY = "http://localhost:1337"


@pytest.mark.parametrize("base_url, location, expected", [
	# the upstream's own URLs come back through the proxy, query and fragment intact
	("https://jira.test", "https://jira.test/browse/A-1?x=1#c", "http://localhost:1337/browse/A-1?x=1#c"),
	("https://jira.test", "https://JIRA.test:443/a", "http://localhost:1337/a"),
	("https://jira.test", "//jira.test/a", "http://localhost:1337/a"),
	("https://jira.test", "https://jira.test", "http://localhost:1337/"),
	("http://jira.test:8080", "http://jira.test:8080/a", "http://localhost:1337/a"),
	# a base URL with a path: only what sits under it, at a segment boundary
	("https://h.test/jira", "https://h.test/jira/rest/x", "http://localhost:1337/rest/x"),
	("https://h.test/jira", "https://h.test/jira", "http://localhost:1337/"),
	("https://h.test/jira", "/jira/rest/x?y=2", "/rest/x?y=2"),
	("https://h.test/jira", "https://h.test/jiramore/x", "https://h.test/jiramore/x"),
	("https://h.test/jira", "https://h.test/other", "https://h.test/other"),
	("https://h.test/jira", "/other?x=1", "https://h.test/other?x=1"),   # only the upstream origin can answer it
	("https://h.test/jira", "/jira//evil.com/", "http://localhost:1337//evil.com/"),   # never a scheme-relative redirect
	("https://h.test/jira", "/jira/\\evil.com", "http://localhost:1337/\\evil.com"),
	("https://jira.test", "https://jira.test//evil.com", "http://localhost:1337//evil.com"),
	# anywhere else is left alone
	("https://jira.test", "https://id.atlassian.com/login", "https://id.atlassian.com/login"),
	("https://jira.test", "http://jira.test/a", "http://jira.test/a"),
	("https://jira.test", "https://jira.test:8443/a", "https://jira.test:8443/a"),
	("https://jira.test", "/a", "/a"),
	("https://jira.test", "a/b", "a/b"),
	("https://jira.test", "https://jira.test:bad/a", "https://jira.test:bad/a"),
])
def test_rewrite_location(base_url, location, expected):
	headers = [("Content-Type", "text/plain"), ("Location", location)]
	assert rewrite_location(headers, base_url, PROXY) == [("Content-Type", "text/plain"), ("Location", expected)]


def test_rewrite_location_leaves_other_headers_and_bodies_alone():
	headers = [("Link", "<https://jira.test/a>; rel=next"), ("content-location", "https://jira.test/a")]
	assert rewrite_location(headers, "https://jira.test", PROXY) == headers
	assert rewrite_location([("location", "https://jira.test/a")], "https://jira.test", PROXY) == [("location", "http://localhost:1337/a")]
