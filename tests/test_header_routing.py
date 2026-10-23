# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""X-WR-Upstream picks the upstream; the path is forwarded untouched.

Without the header, an identity bound to exactly one upstream uses that one. Anonymous
callers must always name the upstream. There is no path-prefix fallback.
"""

import httpx
import pytest

from proxy import db
from proxy.approvals import Decision

ID = {"X-WR-Identity": "jira-uat"}


@pytest.fixture
def bare(make_client):
	"""A client that sends no X-WR-Upstream by default."""
	return make_client(wr_upstream=None)


@pytest.fixture
def conf(admin_db):
	db.put_upstream(admin_db, db.Upstream("conf", "https://conf.test"))


def test_the_header_routes_and_the_path_is_forwarded_untouched(bare, fake_upstream):
	r = bare.get("/rest/api/2/serverInfo", params={"a": "1"}, headers={"X-WR-Upstream": "jira"})
	assert r.status_code == 200
	assert str(fake_upstream.requests[-1].url) == "https://jira.test/rest/api/2/serverInfo?a=1"


def test_the_root_path_and_percent_encoding_survive(bare, fake_upstream):
	bare.get("/", headers={"X-WR-Upstream": "jira"})
	assert str(fake_upstream.requests[-1].url) == "https://jira.test/"
	bare.get("/issue/A%2FB%20C", headers={"X-WR-Upstream": "jira"})
	assert fake_upstream.requests[-1].url.raw_path == b"/issue/A%2FB%20C"


def test_the_header_is_never_forwarded_upstream(bare, fake_upstream):
	bare.get("/x", headers={"X-WR-Upstream": "jira"})
	assert "x-wr-upstream" not in fake_upstream.requests[-1].headers


def test_upstream_ids_match_in_any_letter_case(bare, fake_upstream):
	assert bare.get("/x", headers={"X-WR-Upstream": "JIRA"}).status_code == 200
	assert str(fake_upstream.requests[-1].url) == "https://jira.test/x"


def test_an_identity_bound_to_one_upstream_needs_no_header(bare, add_identity, fake_upstream):
	add_identity()
	assert bare.get("/x", headers=ID).status_code == 200
	assert str(fake_upstream.requests[-1].url) == "https://jira.test/x"
	assert fake_upstream.requests[-1].headers["authorization"].startswith("Basic ")


def test_an_identity_bound_to_several_upstreams_must_choose(bare, conf, add_identity, fake_upstream):
	add_identity(upstream_id=["jira", "conf"])
	r = bare.get("/x", headers=ID)
	assert r.status_code == 400
	assert "identity 'jira-uat' is bound to several upstreams; send X-WR-Upstream with one of: conf, jira" in r.text
	assert fake_upstream.requests == []
	assert bare.get("/x", headers={**ID, "X-WR-Upstream": "conf"}).status_code == 200
	assert str(fake_upstream.requests[-1].url) == "https://conf.test/x"


def test_an_identity_bound_to_no_upstream_is_forbidden(bare, add_identity, fake_upstream):
	add_identity(upstream_id=None)
	r = bare.get("/x", headers=ID)
	assert r.status_code == 403
	assert "identity 'jira-uat' is not bound to any upstream" in r.text
	assert fake_upstream.requests == []


def test_an_unbound_upstream_is_forbidden(bare, conf, add_identity, fake_upstream):
	add_identity()
	r = bare.get("/x", headers={**ID, "X-WR-Upstream": "conf"})
	assert r.status_code == 403
	assert "identity 'jira-uat' is not bound to upstream 'conf'; bound to: jira" in r.text
	assert fake_upstream.requests == []


@pytest.mark.parametrize("headers", [{"X-WR-Upstream": "ghost"}, {**ID, "X-WR-Upstream": "ghost"}])
def test_an_unknown_upstream_lists_the_configured_ones(bare, conf, add_identity, fake_upstream, headers):
	add_identity()
	r = bare.get("/x", headers=headers)
	assert r.status_code == 400
	assert "unknown upstream 'ghost'; configured upstreams: conf, jira" in r.text
	assert fake_upstream.requests == []


def test_an_empty_or_repeated_upstream_header_is_refused(bare, fake_upstream):
	assert bare.get("/x", headers={"X-WR-Upstream": " "}).status_code == 400
	r = bare.get("/x", headers=[("X-WR-Upstream", "jira"), ("X-WR-Upstream", "jira")])
	assert r.status_code == 400 and "send X-WR-Upstream once" in r.text
	assert fake_upstream.requests == []


def test_anonymous_must_name_the_upstream(bare, conf, fake_upstream):
	r = bare.get("/x")
	assert r.status_code == 400
	assert "send X-WR-Upstream to choose an upstream; configured upstreams: conf, jira" in r.text
	assert fake_upstream.requests == []


def test_anonymous_with_the_header_is_cached_without_credentials(bare, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/rest/api/2/serverInfo", 3600)
	up = {"X-WR-Upstream": "jira"}
	assert bare.get("/rest/api/2/serverInfo", headers=up).headers["x-cache"] == "MISS"
	assert bare.get("/rest/api/2/serverInfo", headers=up).headers["x-cache"] == "HIT"
	assert len(fake_upstream.requests) == 1
	assert "authorization" not in fake_upstream.requests[0].headers


def test_anonymous_lan_clients_need_no_approval(make_client, prompter, fake_upstream):
	lan = make_client(client_addr="192.0.2.111", wr_upstream=None)
	assert lan.get("/x", headers={"X-WR-Upstream": "jira"}).status_code == 200
	assert prompter.messages == []


def test_an_inferred_upstream_still_needs_lan_approval(make_client, add_identity, prompter, fake_upstream):
	lan = make_client(client_addr="192.0.2.111", wr_upstream=None)
	add_identity()
	prompter.answer = Decision.NO
	assert lan.get("/x", headers=ID).status_code == 403
	assert fake_upstream.requests == []
	assert "https://jira.test" in prompter.messages[0]


def test_the_old_prefix_is_just_a_path(bare, fake_upstream):
	bare.get("/jira/rest/x", headers={"X-WR-Upstream": "jira"})
	assert str(fake_upstream.requests[-1].url) == "https://jira.test/jira/rest/x"


def test_reserved_paths_are_still_never_proxied(bare, fake_upstream):
	assert bare.get("/_nope", headers={"X-WR-Upstream": "jira"}).status_code == 404
	assert fake_upstream.requests == []


def test_a_redirect_to_the_upstream_comes_back_through_the_proxy(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(
		302, headers={"Location": "https://jira.test/login.jsp?next=%2Fx", "Link": "<https://jira.test/a>"},
		json={"self": "https://jira.test/rest/x"},
	)
	r = client.get("/x", follow_redirects=False)
	assert r.status_code == 302
	assert r.headers["location"] == "http://testserver/login.jsp?next=%2Fx"
	assert r.headers["link"] == "<https://jira.test/a>"           # only Location is rewritten
	assert r.json() == {"self": "https://jira.test/rest/x"}       # and bodies never are


def test_a_201_location_is_rewritten_and_the_host_is_the_callers(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(201, headers={"Location": "https://jira.test/rest/api/2/issue/10001"})
	r = client.post("/rest/api/2/issue", headers={"Host": "127.0.0.1:1337"})
	assert r.headers["location"] == "http://127.0.0.1:1337/rest/api/2/issue/10001"


def test_a_redirect_on_a_cached_path_is_rewritten_and_not_cached(client, admin_db, fake_upstream):
	db.create_rule(admin_db, "jira", "/**", 3600)
	fake_upstream.responder = lambda req: httpx.Response(301, headers={"Location": "https://jira.test/y"})
	r = client.get("/x", follow_redirects=False)
	assert (r.status_code, r.headers["x-cache"], r.headers["location"]) == (301, "MISS", "http://testserver/y")


def test_other_hosts_are_left_alone(client, fake_upstream):
	fake_upstream.responder = lambda req: httpx.Response(302, headers={"Location": "https://id.atlassian.com/login"})
	assert client.get("/x", follow_redirects=False).headers["location"] == "https://id.atlassian.com/login"
