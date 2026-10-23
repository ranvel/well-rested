# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import os
import sys
import uuid

import pytest

from proxy.keychain import PROXY_SERVICE, KeychainStore, MemorySecretStore


def test_memory_store_round_trip():
	store = MemorySecretStore()
	assert store.get("jira-uat") is None
	store.set("jira-uat", "tok")
	assert store.get("jira-uat") == "tok"
	assert store.delete("jira-uat")
	assert not store.delete("jira-uat")


def test_memory_store_repr_hides_tokens():
	store = MemorySecretStore()
	store.set("jira-uat", "tok-secret-123")
	assert "tok-secret-123" not in repr(store)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain only")
def test_keychain_store_never_consults_the_default_keyring(monkeypatch):
	import keyring
	monkeypatch.setattr(keyring, "get_keyring", lambda: pytest.fail("get_keyring() was called"))
	KeychainStore()


@pytest.mark.keychain
@pytest.mark.skipif(os.environ.get("WR_TEST_KEYCHAIN") != "1", reason="set WR_TEST_KEYCHAIN=1 to touch the real Keychain")
def test_real_keychain_round_trip():
	store = KeychainStore(service="well-rested-test")
	name = f"test-{uuid.uuid4().hex[:8]}"
	try:
		store.set(name, "tok-1")
		assert store.get(name) == "tok-1"
		store.set(name, "tok-2")
		assert store.get(name) == "tok-2"
	finally:
		assert store.delete(name)
	assert store.get(name) is None


@pytest.mark.keychain
@pytest.mark.skipif(os.environ.get("WR_TEST_KEYCHAIN") != "1", reason="set WR_TEST_KEYCHAIN=1 to touch the real Keychain")
def test_real_keychain_identity_and_proxy_share_an_account_name():
	identities, proxies = KeychainStore(), KeychainStore(PROXY_SERVICE)
	name = f"test-{uuid.uuid4().hex[:8]}"
	try:
		identities.set(name, "identity-token")
		proxies.set(name, "proxy-password")
		assert identities.get(name) == "identity-token"
		assert proxies.get(name) == "proxy-password"
	finally:
		identities.delete(name)
		proxies.delete(name)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain only")
def test_keychain_store_wraps_backend_errors_with_their_type(monkeypatch):
	from keyring.errors import KeyringLocked, PasswordSetError

	from proxy.keychain import KeychainError

	class LockedBackend:
		def get_password(self, service, name):
			raise KeyringLocked("Can't get password from keychain: (-128, 'Keychain Access Denied')")

		def set_password(self, service, name, token):
			raise PasswordSetError("Can't store password on keychain")

	store = KeychainStore()
	store._backend = LockedBackend()
	with pytest.raises(KeychainError, match="read failed for 'jira-uat'.*KeyringLocked") as info:
		store.get("jira-uat")
	assert isinstance(info.value, RuntimeError)
	assert isinstance(info.value.__cause__, KeyringLocked)
	with pytest.raises(KeychainError, match="write failed.*PasswordSetError"):
		store.set("jira-uat", "tok")
