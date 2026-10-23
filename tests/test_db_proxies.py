# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbound proxy rows, upstreams.proxy, and the proxies_version counter."""

import sqlite3

import pytest

from proxy import db

CORP = db.OutboundProxy("corp", "http://10.0.0.5:3128", username="egress")


@pytest.fixture
def conn(tmp_path):
	c = db.connect(tmp_path / "p.db")
	db.put_upstream(c, db.Upstream("jira", "https://jira.test"))
	yield c
	c.close()


def test_create_get_list_round_trip(conn):
	assert db.create_proxy(conn, CORP) == CORP
	assert db.get_proxy(conn, "corp") == CORP
	db.create_proxy(conn, db.OutboundProxy("a-socks", "socks5h://127.0.0.1:1080"))
	assert [p.name for p in db.list_proxies(conn)] == ["a-socks", "corp"]


def test_duplicate_name_is_refused(conn):
	db.create_proxy(conn, CORP)
	with pytest.raises(ValueError, match="already exists"):
		db.create_proxy(conn, CORP)


@pytest.mark.parametrize("name", ["Corp", "corp_egress", "-corp", "", "x" * 65])
def test_bad_names_are_refused(conn, name):
	with pytest.raises(ValueError, match="invalid proxy name"):
		db.create_proxy(conn, db.OutboundProxy(name, "http://h:1"))


@pytest.mark.parametrize("username", ["", "a:b", "a\nb"])
def test_bad_usernames_are_refused(conn, username):
	with pytest.raises(ValueError, match="username"):
		db.create_proxy(conn, db.OutboundProxy("corp", "http://h:1", username=username))


def test_writes_bump_proxies_version(conn):
	assert db.proxies_version(conn) == 0
	db.create_proxy(conn, CORP)
	assert db.proxies_version(conn) == 1
	db.update_proxy(conn, db.OutboundProxy("corp", "http://10.0.0.6:3128"))
	assert db.proxies_version(conn) == 2
	assert db.get_proxy(conn, "corp").username is None
	assert db.delete_proxy(conn, "corp")
	assert db.proxies_version(conn) == 3


def test_update_of_unknown_proxy_is_lookup_error(conn):
	with pytest.raises(LookupError, match="nope"):
		db.update_proxy(conn, db.OutboundProxy("nope", "http://h:1"))


def test_upstream_proxy_round_trip(conn):
	db.create_proxy(conn, CORP)
	up = db.put_upstream(conn, db.Upstream("jira", "https://jira.test", proxy="corp"))
	assert up.proxy == "corp"
	assert db.upstreams_by_proxy(conn) == {"corp": ["jira"]}
	assert db.put_upstream(conn, db.Upstream("jira", "https://jira.test")).proxy is None   # Direct


def test_upstream_with_unknown_proxy_is_refused(conn):
	with pytest.raises(ValueError, match="no outbound proxy named 'ghost'"):
		db.put_upstream(conn, db.Upstream("jira", "https://jira.test", proxy="ghost"))


def test_delete_in_use_is_refused_naming_the_prefixes(conn):
	db.create_proxy(conn, CORP)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test", proxy="corp"))
	db.put_upstream(conn, db.Upstream("wiki", "https://wiki.test", proxy="corp"))
	with pytest.raises(ValueError, match="/jira, /wiki"):
		db.delete_proxy(conn, "corp")
	assert db.get_proxy(conn, "corp") is not None
	assert db.proxies_version(conn) == 1


def test_delete_unused_and_missing(conn):
	db.create_proxy(conn, CORP)
	assert db.delete_proxy(conn, "corp") is True
	assert db.delete_proxy(conn, "corp") is False


def test_foreign_key_restrict_is_the_backstop(conn):
	db.create_proxy(conn, CORP)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test", proxy="corp"))
	with pytest.raises(sqlite3.IntegrityError):
		conn.execute("DELETE FROM proxies WHERE name = 'corp'")
