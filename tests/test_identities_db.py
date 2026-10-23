# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime, timedelta, timezone

import pytest

from proxy import db

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	db.put_upstream(conn, db.Upstream("wiki", "https://wiki.test"))
	yield conn
	conn.close()


def add(conn, name="jira-uat", **overrides):
	fields = dict(
		name=name, scheme="basic", username="ran@example.com",
		issued_at=NOW, expires_at=NOW + timedelta(days=30),
		rotate_url="https://id.atlassian.com/manage-profile/security/api-tokens",
	)
	fields.update(overrides)
	return db.create_identity(conn, **fields)


def test_round_trip(conn):
	identity = add(conn)
	assert identity == db.get_identity(conn, "jira-uat")
	assert identity.expires_at == NOW + timedelta(days=30)
	assert identity.last_status is None
	assert identity.cache_identity == f"jira-uat#{identity.id}"
	assert identity.upstream_ids == ()   # created first, bound to upstreams later


@pytest.mark.parametrize("name", ["", "-x", "has space", "a/b", "x" * 65, "anonymous", "Anonymous"])
def test_bad_names_are_refused(conn, name):
	with pytest.raises(ValueError):
		add(conn, name=name)


def test_names_are_unique_ignoring_case(conn):
	add(conn)
	with pytest.raises(ValueError, match="already exists"):
		add(conn, name="JIRA-UAT")


@pytest.mark.parametrize("overrides", [
	{"username": None},
	{"username": "a:b"},
	{"username": "a\nb"},
	{"scheme": "bearer"},            # bearer takes no username
	{"scheme": "digest"},
	{"expires_at": NOW},             # must be after issued_at
	{"issued_at": NOW.replace(tzinfo=None)},
	{"rotate_url": "ftp://x"},
	{"rotate_url": 'https://x/"y'},
])
def test_invalid_fields_are_refused(conn, overrides):
	with pytest.raises(ValueError):
		add(conn, **overrides)


def test_bearer_identity(conn):
	assert add(conn, scheme="bearer", username=None).scheme == "bearer"


def test_custom_identity_takes_no_username(conn):
	assert add(conn, scheme="custom", username=None, header="Authorization").scheme == "custom"
	with pytest.raises(ValueError, match="custom identity takes no username"):
		add(conn, name="other", scheme="custom", username="ran@example.com", header="Authorization")


def test_custom_identity_names_its_header_as_typed(conn):
	identity = add(conn, scheme="custom", username=None, header="X-Api-Key")
	assert identity.header == "X-Api-Key"
	assert db.get_identity(conn, "jira-uat").header == "X-Api-Key"
	assert add(conn, name="site", scheme="custom", username=None, header="Cookie").header == "Cookie"
	assert add(conn, name="svc", scheme="bearer", username=None).header is None


@pytest.mark.parametrize("header", [
	None, "", "Host", "content-length", "Accept-Encoding", "X-WR-Identity", "x-wr-upstream",
	"Connection", "Proxy-Authorization", "Transfer-Encoding", "bad name", "foo:bar", "x" * 65, "caf\u00e9",
])
def test_custom_identity_refuses_a_missing_or_reserved_header_name(conn, header):
	with pytest.raises(ValueError, match="header name"):
		add(conn, scheme="custom", username=None, header=header)


@pytest.mark.parametrize("scheme, username", [("basic", "ran@example.com"), ("bearer", None)])
def test_only_a_custom_identity_takes_a_header_name(conn, scheme, username):
	with pytest.raises(ValueError, match=f"a {scheme} identity takes no header name"):
		add(conn, scheme=scheme, username=username, header="x-api-key")


def test_version_bumps_on_config_changes_only(conn):
	v0 = db.identities_version(conn)
	identity = add(conn)
	assert db.identities_version(conn) == v0 + 1
	db.set_identity_status(conn, identity.id, "rejected", NOW)
	assert db.identities_version(conn) == v0 + 1
	db.rotate_identity(conn, "jira-uat", issued_at=NOW, expires_at=NOW + timedelta(days=7))
	assert db.identities_version(conn) == v0 + 2
	db.delete_identity(conn, "jira-uat")
	assert db.identities_version(conn) == v0 + 3


def test_rotation_keeps_the_row_and_clears_status(conn):
	identity = add(conn)
	db.set_identity_status(conn, identity.id, "rejected", NOW)
	later = NOW + timedelta(days=1)
	rotated = db.rotate_identity(conn, "jira-uat", issued_at=later, expires_at=later + timedelta(days=30))
	assert rotated.id == identity.id
	assert rotated.issued_at == later
	assert rotated.last_status is None
	assert rotated.rotate_url == identity.rotate_url


def test_rotating_an_unknown_identity_raises(conn):
	with pytest.raises(LookupError):
		db.rotate_identity(conn, "ghost", issued_at=NOW, expires_at=NOW + timedelta(days=1))


def test_recreated_identity_gets_a_new_id(conn):
	first = add(conn)
	assert db.delete_identity(conn, "jira-uat")
	assert add(conn).id != first.id
	assert not db.delete_identity(conn, "ghost")


def test_approvals(conn):
	identity = add(conn)
	assert not db.has_approval(conn, "192.0.2.111", identity.id)
	db.put_approval(conn, "192.0.2.111", identity.id)
	db.put_approval(conn, "192.0.2.111", identity.id)   # idempotent
	assert db.has_approval(conn, "192.0.2.111", identity.id)
	[approval] = db.list_approvals(conn)
	assert (approval.client_addr, approval.identity) == ("192.0.2.111", "jira-uat")
	assert db.delete_approval(conn, "192.0.2.111", "jira-uat")
	assert not db.delete_approval(conn, "192.0.2.111", "jira-uat")


def test_deleting_an_identity_cascades_to_its_approvals(conn):
	identity = add(conn)
	db.put_approval(conn, "192.0.2.111", identity.id)
	db.delete_identity(conn, "jira-uat")
	assert db.list_approvals(conn) == []


def test_binding_an_identity_to_several_upstreams(conn):
	add(conn)
	add(conn, name="svc")
	db.set_upstream_identities(conn, "jira", ["jira-uat", "svc"])
	db.set_upstream_identities(conn, "wiki", ["JIRA-UAT"])   # names match ignoring case
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("jira", "wiki")
	assert db.get_identity(conn, "svc").upstream_ids == ("jira",)
	assert [i.upstream_ids for i in db.list_identities(conn)] == [("jira", "wiki"), ("jira",)]
	assert db.identities_by_upstream(conn) == {"jira": ["jira-uat", "svc"], "wiki": ["jira-uat"]}


def test_setting_bindings_replaces_the_upstreams_whole_set(conn):
	add(conn)
	add(conn, name="svc")
	db.set_upstream_identities(conn, "jira", ["jira-uat", "svc"])
	db.set_upstream_identities(conn, "jira", ["svc"])
	assert db.identities_by_upstream(conn) == {"jira": ["svc"]}
	db.set_upstream_identities(conn, "jira", [])
	assert db.identities_by_upstream(conn) == {}


def test_bindings_bump_the_version_only_when_they_change(conn):
	add(conn)
	v0 = db.identities_version(conn)
	db.set_upstream_identities(conn, "jira", ["jira-uat"])
	assert db.identities_version(conn) == v0 + 1
	db.set_upstream_identities(conn, "jira", ["jira-uat"])   # unchanged: no Keychain reload for nothing
	assert db.identities_version(conn) == v0 + 1
	db.set_upstream_identities(conn, "jira", [])
	assert db.identities_version(conn) == v0 + 2


@pytest.mark.parametrize("upstream_id, names, fragment", [
	("jira", ["jira-uat", "ghost", "nobody"], "no identity named 'ghost', 'nobody'"),
	("nope", ["jira-uat"], "no upstream 'nope'"),
])
def test_bad_bindings_are_refused_and_change_nothing(conn, upstream_id, names, fragment):
	add(conn)
	db.set_upstream_identities(conn, "wiki", ["jira-uat"])
	v0 = db.identities_version(conn)
	with pytest.raises(ValueError, match=fragment):
		db.set_upstream_identities(conn, upstream_id, names)
	assert db.identities_by_upstream(conn) == {"wiki": ["jira-uat"]}
	assert db.identities_version(conn) == v0


def test_deleting_an_identity_removes_its_bindings(conn):
	add(conn)
	db.set_upstream_identities(conn, "jira", ["jira-uat"])
	db.delete_identity(conn, "jira-uat")
	assert db.identities_by_upstream(conn) == {}


def test_deleting_an_upstream_unbinds_its_identities_and_bumps_the_version(conn):
	identity = add(conn)
	db.put_approval(conn, "192.0.2.111", identity.id)
	db.set_upstream_identities(conn, "jira", ["jira-uat"])
	db.set_upstream_identities(conn, "wiki", ["jira-uat"])
	v0 = db.identities_version(conn)
	assert db.delete_upstream(conn, "jira")
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("wiki",)
	assert len(db.list_approvals(conn)) == 1   # the identity and its approvals are untouched
	assert db.identities_version(conn) == v0 + 1
	db.delete_upstream(conn, "wiki")
	assert db.get_identity(conn, "jira-uat").upstream_ids == ()


def test_deleting_an_unbound_upstream_leaves_the_version_alone(conn):
	v0 = db.identities_version(conn)
	assert db.delete_upstream(conn, "wiki")
	assert db.identities_version(conn) == v0


def test_put_upstream_can_set_bindings_in_the_same_write(conn):
	add(conn)
	db.put_upstream(conn, db.Upstream("conf", "https://conf.test"), identities=["jira-uat"])
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("conf",)
	db.put_upstream(conn, db.Upstream("conf", "https://conf2.test"))   # None: bindings unchanged
	assert db.get_identity(conn, "jira-uat").upstream_ids == ("conf",)
	with pytest.raises(ValueError, match="ghost"):
		db.put_upstream(conn, db.Upstream("conf", "https://conf3.test"), identities=["ghost"])
	assert db.get_upstream(conn, "conf").base_url == "https://conf2.test"   # all or nothing
	with pytest.raises(ValueError, match="ghost"):
		db.put_upstream(conn, db.Upstream("brand-new", "https://new.test"), identities=["ghost"])
	assert db.get_upstream(conn, "brand-new") is None
