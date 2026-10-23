# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import base64
import io
from datetime import datetime, timedelta, timezone

import pytest

from proxy import db
from proxy.identities import IdentityRegistry, health, validate_token, warning
from proxy.keychain import MemorySecretStore
from proxy.log import configure

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ROTATE = "https://id.atlassian.com/manage-profile/security/api-tokens"


@pytest.fixture
def conn(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	yield conn
	conn.close()


@pytest.fixture
def secrets():
	return MemorySecretStore()


def add(conn, secrets, name="jira-uat", token="tok-1", days_left=20, lifetime=30, **overrides):
	fields = dict(
		name=name, scheme="basic", username="ran@example.com",
		issued_at=NOW - timedelta(days=lifetime - days_left), expires_at=NOW + timedelta(days=days_left),
		rotate_url=ROTATE,
	)
	fields.update(overrides)
	identity = db.create_identity(conn, **fields)
	if token is not None:
		secrets.set(name, token)
	return identity


@pytest.mark.parametrize("days_left, level", [(29, "green"), (12, "amber"), (3, "red")])
def test_health_levels(conn, secrets, days_left, level):
	result = health(add(conn, secrets, days_left=days_left, lifetime=30), NOW)
	assert result.level == level
	assert result.fraction == pytest.approx(days_left / 30)


@pytest.mark.parametrize("past_expiry", [timedelta(), timedelta(days=5)])
def test_expired_at_or_past_expiry(conn, secrets, past_expiry):
	identity = add(conn, secrets, days_left=10, lifetime=30)
	result = health(identity, identity.expires_at + past_expiry)
	assert (result.level, result.fraction) == ("expired", 0.0)


def test_rejected_overrides_the_date(conn, secrets):
	identity = add(conn, secrets, days_left=29)
	db.set_identity_status(conn, identity.id, "rejected", NOW)
	assert health(db.get_identity(conn, "jira-uat"), NOW).level == "rejected"


def test_warning_texts(conn, secrets):
	assert warning(add(conn, secrets, name="fresh", days_left=29), NOW) is None
	red = add(conn, secrets, name="red", days_left=3)
	assert warning(red, NOW) == f'299 - "token for \'red\' expires 2026-10-13; rotate at {ROTATE}"'
	bare = add(conn, secrets, name="bare", days_left=3, rotate_url=None)
	assert warning(bare, NOW) == '299 - "token for \'bare\' expires 2026-10-13"'
	db.set_identity_status(conn, red.id, "rejected", NOW)
	assert warning(db.get_identity(conn, "red"), NOW) == (
		f'299 - "token for \'red\' was rejected by the upstream (401); rotate at {ROTATE}"'
	)


@pytest.mark.parametrize("scheme", ["basic", "bearer"])
@pytest.mark.parametrize("raw, expected", [("tok\n", "tok"), ("  tok  ", "tok")])
def test_validate_token_strips(raw, expected, scheme):
	assert validate_token(raw, scheme) == expected


@pytest.mark.parametrize("scheme", ["basic", "bearer"])
@pytest.mark.parametrize("raw", ["", "   ", "a b", "a\tb", "a\x00b"])
def test_validate_token_refuses(raw, scheme):
	with pytest.raises(ValueError):
		validate_token(raw, scheme)


@pytest.mark.parametrize("scheme", ["basic", "bearer", "custom"])
def test_validate_token_refuses_what_a_header_cannot_carry(scheme):
	with pytest.raises(ValueError, match="latin-1"):   # would raise UnicodeEncodeError at send time otherwise
		validate_token("tok-\u20ac", scheme)
	assert validate_token("tok-\u00e9", scheme) == "tok-\u00e9"   # latin-1 is the header charset httpx encodes with


@pytest.mark.parametrize("raw, expected", [
	("Token abc123\n", "Token abc123"),
	("  ApiKey key=abc, sig=def  ", "ApiKey key=abc, sig=def"),
	("opaque-value", "opaque-value"),
])
def test_custom_header_value_keeps_inner_spaces(raw, expected):
	assert validate_token(raw, "custom") == expected


@pytest.mark.parametrize("raw", ["", "   ", "Token abc\r\nX-Evil: 1", "Token\tabc", "Token a\x00b", "Token a\x7fb"])
def test_custom_header_value_refuses_empty_and_control_characters(raw):
	with pytest.raises(ValueError, match="header value"):
		validate_token(raw, "custom")


async def test_refresh_loads_tokens_and_builds_headers(conn, secrets):
	add(conn, secrets)
	add(conn, secrets, name="bearer-one", scheme="bearer", username=None, token="tok-b")
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	basic = registry.resolve("jira-uat")
	assert registry.credential(basic) == ("Authorization", "Basic " + base64.b64encode(b"ran@example.com:tok-1").decode())
	assert registry.credential(registry.resolve("bearer-one")) == ("Authorization", "Bearer tok-b")
	add(conn, secrets, name="custom-one", scheme="custom", username=None, header="Authorization", token="Token tok-c")
	add(conn, secrets, name="claude", scheme="custom", username=None, header="x-api-key", token="sk-1")
	await registry.refresh()
	assert registry.credential(registry.resolve("custom-one")) == ("Authorization", "Token tok-c")   # sent verbatim
	assert registry.credential(registry.resolve("claude")) == ("x-api-key", "sk-1")   # under the identity's own header
	assert registry.resolve("ghost") is None


async def test_missing_token_is_reported_not_fatal(conn, secrets):
	add(conn, secrets, token=None)
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	identity = registry.resolve("jira-uat")
	assert identity is not None
	assert not registry.has_token(identity)
	assert registry.credential(identity) is None


async def test_refresh_reloads_only_when_the_version_changes(conn, secrets):
	add(conn, secrets)
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	reads = secrets.reads
	await registry.refresh()
	assert secrets.reads == reads
	add(conn, secrets, name="second")
	await registry.refresh()
	assert registry.resolve("second") is not None
	assert secrets.reads > reads


async def test_record_status_writes_on_change_only(conn, secrets):
	add(conn, secrets)
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	identity = registry.record_status(registry.resolve("jira-uat"), 401, now=NOW)
	assert identity.last_status == "rejected"
	assert db.get_identity(conn, "jira-uat").last_status_at == NOW
	registry.record_status(identity, 401, now=NOW + timedelta(hours=1))
	assert db.get_identity(conn, "jira-uat").last_status_at == NOW   # unchanged: no write
	assert registry.record_status(identity, 500, now=NOW).last_status == "rejected"   # not a token signal
	assert registry.record_status(identity, 403, now=NOW).last_status == "rejected"   # permissions, not the token
	assert registry.record_status(identity, 204, now=NOW).last_status == "ok"


async def test_registry_repr_hides_tokens(conn, secrets):
	add(conn, secrets, token="tok-secret-123")
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	assert "tok-secret-123" not in repr(registry)


class FailingStore(MemorySecretStore):
	"""A store whose get() raises for the names in .failing, as a locked Keychain would."""

	def __init__(self):
		super().__init__()
		self.failing: set[str] = set()

	def get(self, name):
		if name in self.failing:
			raise RuntimeError("Can't get password from keychain: (-128, 'Keychain Access Denied')")
		return super().get(name)


async def test_a_failed_keychain_read_is_a_missing_token_for_that_identity_only(conn):
	logged = io.StringIO()
	configure("DEBUG", stream=logged)
	secrets = FailingStore()
	add(conn, secrets, name="locked", token="tok-locked")
	add(conn, secrets, name="fine", token="tok-fine")
	secrets.failing.add("locked")
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	locked, fine = registry.resolve("locked"), registry.resolve("fine")
	assert not registry.has_token(locked) and registry.credential(locked) is None
	assert registry.load_error(locked) == "RuntimeError"
	assert registry.has_token(fine) and registry.load_error(fine) is None
	err = logged.getvalue()
	assert "identity=locked" in err and "RuntimeError" in err
	assert "Access Denied" not in err   # the type only, never the message


async def test_a_failed_read_is_retried_at_the_next_loading_step(conn):
	secrets = FailingStore()
	add(conn, secrets, name="locked", token="tok-locked")
	secrets.failing.add("locked")
	registry = IdentityRegistry(conn, secrets)
	await registry.refresh()
	await registry.refresh()   # same version: not retried, still failed
	assert registry.load_error(registry.resolve("locked")) == "RuntimeError"
	secrets.failing.clear()
	db.rotate_identity(conn, "locked", issued_at=NOW, expires_at=NOW + timedelta(days=30))
	await registry.refresh()
	locked = registry.resolve("locked")
	assert registry.load_error(locked) is None
	assert registry.credential(locked) == ("Authorization", "Basic " + base64.b64encode(b"ran@example.com:tok-locked").decode())
