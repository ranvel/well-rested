# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbound proxy validation, the Keychain-ordered operations, and the runtime registry."""

import certifi
import pytest

from proxy import db
from proxy.keychain import PROXY_SERVICE, MemorySecretStore
from proxy.outbound import (
	OutboundProxies, OutboundProxyFailure, Route, add_proxy, check_ca_bundle, edit_proxy,
	normalize_url, remove_proxy, tls_context,
)


@pytest.fixture
def conn(tmp_path):
	c = db.connect(tmp_path / "p.db")
	db.put_upstream(c, db.Upstream("jira", "https://jira.test"))
	yield c
	c.close()


@pytest.fixture
def store():
	return MemorySecretStore()


async def add(conn, store, **overrides):
	fields = dict(name="corp", url="http://10.0.0.5:3128", username="egress", password="pw-secret-1")
	fields.update(overrides)
	return await add_proxy(conn, store, **fields)


# --- URL and CA bundle validation ---

@pytest.mark.parametrize("raw, expected", [
	("http://10.0.0.5:3128", "http://10.0.0.5:3128"),
	(" https://proxy.corp:443/ ", "https://proxy.corp"),       # httpx drops a scheme's default port
	("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
	("socks5h://gw.client.test:1080", "socks5h://gw.client.test:1080"),
	("http://[::1]:3128", "http://[::1]:3128"),
])
def test_normalize_url_accepts(raw, expected):
	assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["socks5://127.0.0.1", "socks5h://gw.client.test"])
def test_socks_url_without_a_port_is_rejected(raw):
	with pytest.raises(ValueError, match="a SOCKS proxy URL needs a port, e.g. socks5://host:1080"):
		normalize_url(raw)


def test_userinfo_is_rejected_without_echoing_it():
	with pytest.raises(ValueError) as caught:
		normalize_url("http://egress:hunter2@10.0.0.5:3128")
	assert "Username/Password" in str(caught.value)
	assert "hunter2" not in str(caught.value) and "egress" not in str(caught.value)


@pytest.mark.parametrize("raw, fragment", [
	("ftp://h:21", "unsupported proxy scheme"),
	("http://h:3128/path", "no path or query"),
	("http://h:3128?x=1", "no path or query"),
	("http://:3128", "needs a host"),
])
def test_normalize_url_rejects(raw, fragment):
	with pytest.raises(ValueError, match=fragment):
		normalize_url(raw)


def test_ca_bundle_checks(tmp_path):
	assert check_ca_bundle(None) is None
	assert check_ca_bundle("  ") is None
	assert check_ca_bundle(certifi.where()) == certifi.where()
	with pytest.raises(ValueError, match="absolute path"):
		check_ca_bundle("ca.pem")
	with pytest.raises(ValueError, match="cannot load CA bundle"):
		check_ca_bundle(str(tmp_path / "missing.pem"))
	junk = tmp_path / "junk.pem"
	junk.write_text("not a certificate")
	with pytest.raises(ValueError, match="cannot load CA bundle"):
		check_ca_bundle(str(junk))


def test_tls_context_loads_the_bundle_only_when_verifying(tmp_path):
	missing = str(tmp_path / "gone.pem")
	with pytest.raises(OSError):
		tls_context(True, missing)            # proves the bundle is loaded when verifying
	assert tls_context(False, missing).verify_mode.name == "CERT_NONE"
	assert tls_context(True, certifi.where()).verify_mode.name == "CERT_REQUIRED"


# --- Route ---

def test_route_builds_the_httpx_proxy_and_hides_the_password():
	route = Route("corp", "http://10.0.0.5:3128", "egress", True, None, 3, password="pw-secret-1")
	proxy = route.httpx_proxy()
	assert str(proxy.url) == "http://10.0.0.5:3128"
	assert proxy.auth == ("egress", "pw-secret-1")
	assert proxy.ssl_context is None                       # only https:// proxies get one
	assert "pw-secret-1" not in repr(route)
	assert route.label == "outbound proxy 'corp' (http://10.0.0.5:3128)"
	assert Route("open", "https://p.test:443", None, False, None, 1).httpx_proxy().auth is None
	assert Route("open", "https://p.test:443", None, False, None, 1).httpx_proxy().ssl_context is not None


# --- add / edit / remove ---

async def test_add_puts_the_password_in_the_keychain_only(conn, store):
	created = await add(conn, store)
	assert created == db.OutboundProxy("corp", "http://10.0.0.5:3128", username="egress")
	assert store.get("corp") == "pw-secret-1"
	assert "pw-secret-1" not in "\n".join(conn.iterdump())


async def test_add_refuses_duplicates_and_orphan_passwords(conn, store):
	await add(conn, store)
	with pytest.raises(ValueError, match="already exists"):
		await add(conn, store, password="other")
	assert store.get("corp") == "pw-secret-1"              # the refused add didn't overwrite it
	with pytest.raises(ValueError, match="needs a username"):
		await add(conn, store, name="anon", username=None, password="pw")
	assert store.get("anon") is None


async def test_add_with_username_but_no_password_is_allowed(conn, store):
	created = await add(conn, store, password="")
	assert created.username == "egress" and store.get("corp") is None


async def test_keychain_failure_leaves_no_row(conn):
	class Refusing(MemorySecretStore):
		def set(self, name, token):
			raise RuntimeError("Keychain write failed for 'corp': KeyringLocked: locked")
	with pytest.raises(RuntimeError):
		await add(conn, Refusing())
	assert db.get_proxy(conn, "corp") is None


async def test_add_rollback_keeps_the_item_when_a_concurrent_add_created_the_row(conn):
	class Racing(MemorySecretStore):
		def set(self, name, token):
			super().set(name, token)
			# the concurrent add finishes its own Keychain write and row between our check and create
			db.create_proxy(conn, db.OutboundProxy("corp", "http://10.0.0.9:3128", "egress"))
	store = Racing()
	with pytest.raises(ValueError, match="already exists"):
		await add(conn, store)
	assert store.get("corp") == "pw-secret-1"
	assert db.get_proxy(conn, "corp").url == "http://10.0.0.9:3128"


async def test_add_rollback_failure_does_not_mask_the_original_error(conn, monkeypatch):
	class Broken(MemorySecretStore):
		def delete(self, name):
			raise RuntimeError("Keychain delete failed")
	def explode(*args, **kwargs):
		raise ValueError("db exploded")
	monkeypatch.setattr(db, "create_proxy", explode)
	with pytest.raises(ValueError, match="db exploded"):
		await add(conn, Broken())


async def test_verify_tls_is_kept_on_for_non_https(conn, store):
	created = await add(conn, store, verify_tls=False)
	assert created.verify_tls is True                      # the page's disabled checkbox submits nothing
	edited = await edit_proxy(conn, store, "corp", url="https://p.test:443", username="egress", password="", verify_tls=False)
	assert edited.verify_tls is False


async def test_edit_blank_password_keeps_the_secret(conn, store):
	await add(conn, store)
	await edit_proxy(conn, store, "corp", url="http://10.0.0.6:3128", username="egress", password="")
	assert store.get("corp") == "pw-secret-1"
	await edit_proxy(conn, store, "corp", url="http://10.0.0.6:3128", username="egress", password="pw-secret-2")
	assert store.get("corp") == "pw-secret-2"


async def test_edit_clearing_the_username_drops_the_password(conn, store):
	await add(conn, store)
	edited = await edit_proxy(conn, store, "corp", url="http://10.0.0.5:3128", username=None, password="")
	assert edited.username is None and store.get("corp") is None


async def test_edit_of_unknown_proxy_is_lookup_error(conn, store):
	with pytest.raises(LookupError):
		await edit_proxy(conn, store, "nope", url="http://h:1", username=None, password="")


async def test_remove_in_use_touches_nothing(conn, store):
	await add(conn, store)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test", proxy="corp"))
	with pytest.raises(ValueError, match="/"):
		await remove_proxy(conn, store, "corp")
	assert store.get("corp") == "pw-secret-1" and db.get_proxy(conn, "corp") is not None


async def test_remove_deletes_item_then_row(conn, store):
	await add(conn, store)
	proxy, had_item = await remove_proxy(conn, store, "corp")
	assert (proxy.name, had_item) == ("corp", True)
	assert store.get("corp") is None and db.get_proxy(conn, "corp") is None


# --- the registry ---

async def test_registry_loads_passwords_and_reports_each_reload(conn, store):
	await add(conn, store)
	seen = []

	async def on_reload(generation):
		seen.append(generation)

	registry = OutboundProxies(conn, store, on_reload=on_reload)
	await registry.refresh()
	await registry.refresh()                                # no change, no reload
	route = registry.route("corp")
	assert (route.password, route.generation) == ("pw-secret-1", 1)
	assert registry.route(None) is None                     # Direct
	assert seen == [1]
	assert registry.password_state("corp") == "stored"
	assert "pw-secret-1" not in repr(registry)


async def test_missing_password_is_503_naming_the_proxy(conn, store):
	await add(conn, store, password="")
	registry = OutboundProxies(conn, store)
	await registry.refresh()
	assert registry.password_state("corp") == "missing"
	with pytest.raises(OutboundProxyFailure) as caught:
		registry.route("corp")
	assert caught.value.status == 503
	assert "outbound proxy 'corp'" in str(caught.value) and "no password in the Keychain" in str(caught.value)
	assert "egress" not in str(caught.value) and PROXY_SERVICE not in str(caught.value)
	assert str(caught.value) == (
		"outbound proxy 'corp' (http://10.0.0.5:3128) has no password in the Keychain; set it on the Proxies page"
	)


async def test_missing_password_logs_username_and_keychain_item_at_warning(conn, store, caplog):
	await add(conn, store, password="")
	registry = OutboundProxies(conn, store)
	await registry.refresh()
	caplog.clear()
	with caplog.at_level("WARNING"), pytest.raises(OutboundProxyFailure):
		registry.route("corp")
	text = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
	assert "username=egress" in text and PROXY_SERVICE in text and "account=corp" in text


async def test_keychain_read_failure_is_503_with_its_type(conn, store):
	await add(conn, store)

	class Locked(MemorySecretStore):
		def get(self, name):
			raise RuntimeError("Keychain read failed") from KeyError("locked")

	registry = OutboundProxies(conn, Locked())
	await registry.refresh()
	assert registry.password_state("corp") == "unreadable"
	assert registry.load_error("corp") == "KeyError"
	with pytest.raises(OutboundProxyFailure, match="KeyError") as caught:
		registry.route("corp")
	assert caught.value.status == 503
	assert "egress" not in str(caught.value) and PROXY_SERVICE not in str(caught.value)
	assert "the Keychain read failed (KeyError); set it on the Proxies page" in str(caught.value)


async def test_unknown_proxy_never_means_direct(conn, store):
	registry = OutboundProxies(conn, store)
	await registry.refresh()
	with pytest.raises(OutboundProxyFailure) as caught:
		registry.route("ghost")
	assert caught.value.status == 503


async def test_proxy_without_auth_needs_no_keychain(conn):
	class NoReads(MemorySecretStore):
		def get(self, name):
			raise AssertionError("read the Keychain for a proxy with no username")
	await add_proxy(conn, NoReads(), name="open", url="http://h:1", username=None, password="")
	registry = OutboundProxies(conn, NoReads())
	await registry.refresh()
	assert registry.route("open").password is None
	assert registry.password_state("open") == "none"
