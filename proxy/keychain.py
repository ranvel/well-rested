# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Token storage. The macOS Keychain is the only place a token is ever written.

Items are generic passwords: service "well-rested" (identity tokens, account = identity name) or
"well-rested-proxy" (outbound proxy passwords, account = proxy name). Only
keyring's macOS backend is used; keyring.get_keyring() could resolve to a plaintext
or chained backend, so it is never consulted. The Keychain ACL trusts the python3
binary that created an item, so a Python upgrade brings one permission prompt.
"""

from typing import Protocol

from proxy.log import get_logger

log = get_logger(__name__, "KEYCH")

SERVICE = "well-rested"
PROXY_SERVICE = "well-rested-proxy"   # outbound proxy passwords; account = proxy name


class KeychainError(RuntimeError):
	"""A Keychain call failed (locked, access denied, or another Security framework error).
	The message names the underlying keyring error type; the cause carries the original."""


class SecretStore(Protocol):
	def get(self, name: str) -> str | None: ...
	def set(self, name: str, token: str) -> None: ...
	def delete(self, name: str) -> bool: ...


class KeychainStore:
	def __init__(self, service: str = SERVICE):
		try:
			from keyring.backends import macOS
			from keyring.errors import PasswordDeleteError
		except ImportError as exc:
			raise RuntimeError("the keyring package is required: python3 -m pip install --user keyring") from exc
		if not macOS.Keyring.viable:
			raise RuntimeError("the macOS Keychain is not available on this system")
		self._backend = macOS.Keyring()
		self._delete_error = PasswordDeleteError
		self._service = service

	def get(self, name: str) -> str | None:
		try:
			return self._backend.get_password(self._service, name)
		except Exception as exc:
			raise KeychainError(f"Keychain read failed for '{name}': {type(exc).__name__}: {exc}") from exc

	def set(self, name: str, token: str) -> None:
		try:
			self._backend.set_password(self._service, name, token)
		except Exception as exc:
			raise KeychainError(f"Keychain write failed for '{name}': {type(exc).__name__}: {exc}") from exc
		log.debug("secret stored; service=%s account=%s", self._service, name)

	def delete(self, name: str) -> bool:
		try:
			self._backend.delete_password(self._service, name)
		except self._delete_error:
			return False
		log.debug("secret deleted; service=%s account=%s", self._service, name)
		return True


class MemorySecretStore:
	"""In-process store for tests. Records reads so tests can count Keychain trips."""

	def __init__(self):
		self._tokens: dict[str, str] = {}
		self.reads = 0

	def __repr__(self) -> str:
		return f"MemorySecretStore(names={sorted(self._tokens)})"

	def get(self, name: str) -> str | None:
		self.reads += 1
		return self._tokens.get(name)

	def set(self, name: str, token: str) -> None:
		self._tokens[name] = token

	def delete(self, name: str) -> bool:
		return self._tokens.pop(name, None) is not None
