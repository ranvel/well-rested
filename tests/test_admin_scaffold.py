# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The /_admin scaffolding: mount order, loopback-only, no-store, the page/API pattern."""

import pytest


def test_admin_index_redirects_to_rules(client):
	for path in ("/_admin", "/_admin/"):
		r = client.get(path, follow_redirects=False)
		assert r.status_code == 303
		assert r.headers["location"] == "/_admin/rules"


def test_rules_page_renders_empty_state(client):
	r = client.get("/_admin/rules")
	assert r.status_code == 200
	assert r.headers["content-type"].startswith("text/html")
	assert "<h1>Rules</h1>" in r.text
	assert "No rules yet" in r.text
	for href in ("/_admin/rules", "/_admin/cache", "/_admin/upstreams", "/_admin/identities", "/_admin/approvals", "/_admin/test"):
		assert f'href="{href}"' in r.text


def test_api_rules_empty(client):
	r = client.get("/_admin/api/rules")
	assert r.status_code == 200
	assert r.headers["content-type"].startswith("application/json")
	assert r.json() == {"rules": []}


@pytest.mark.parametrize("name, kind", [("htmx.min.js", "javascript"), ("admin.js", "javascript"), ("style.css", "text/css")])
def test_static_served(client, name, kind):
	r = client.get(f"/_admin/static/{name}")
	assert r.status_code == 200
	assert kind in r.headers["content-type"]
	assert len(r.content) > 100


def test_htmx_is_vendored_and_pinned(client):
	banner = client.get("/_admin/static/htmx.min.js").text[:300]
	assert "htmx 2." in banner and "unpkg.com/htmx.org@2." in banner


def test_admin_script_is_loaded_after_htmx(client):
	html = client.get("/_admin/rules").text
	assert '<script src="/_admin/static/admin.js" defer></script>' in html
	assert html.index("htmx.min.js") < html.index("admin.js")


def test_no_cdn_references(client):
	html = client.get("/_admin/rules").text
	assert "://" not in html.replace("http://localhost", "")   # every script and stylesheet is local


def test_proxy_still_wins_outside_admin(client, fake_upstream):
	assert client.get("/x").headers["x-cache"] == "PASS"
	assert len(fake_upstream.requests) == 1
	assert client.get("/_admin/rules").status_code == 200
	assert len(fake_upstream.requests) == 1


def test_lan_client_gets_403(make_client, fake_upstream):
	lan = make_client(client_addr="192.0.2.111")
	for path in ("/_admin", "/_admin/rules", "/_admin/api/rules", "/_admin/static/htmx.min.js"):
		r = lan.get(path, follow_redirects=False)
		assert r.status_code == 403, path
	assert fake_upstream.requests == []


def test_admin_handlers_send_no_cache_headers(client):
	for path in ("/_admin/rules", "/_admin/api/rules"):
		assert client.get(path).headers["cache-control"] == "no-store"


def test_proxy_route_still_refuses_reserved_prefixes(client, fake_upstream):
	assert client.get("/_internal/x").status_code == 404
	assert fake_upstream.requests == []


def test_favicon_is_linked_from_admin_pages(client):
	assert '<link rel="icon" type="image/png" href="/_admin/static/favicon.png">' in client.get("/_admin/rules").text


@pytest.mark.parametrize("path", ["/favicon.ico", "/_admin/static/favicon.png"])
def test_favicon_served_instead_of_a_502(client, path):
	r = client.get(path)
	assert r.status_code == 200
	assert r.headers["content-type"] == "image/png"
	assert r.content.startswith(b"\x89PNG")
