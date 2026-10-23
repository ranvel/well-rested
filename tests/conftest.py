# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import contextlib
import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy import db, upstream
from proxy.approvals import Decision
from proxy.config import Settings
from proxy.keychain import MemorySecretStore
from proxy.main import create_app


class ClientAddr:
	"""ASGI shim that sets the socket peer address. TestClient otherwise reports 'testclient'.
	Attribute access falls through to the wrapped app, so client.app.state still works."""

	def __init__(self, app, addr: str):
		self.app = app
		self.addr = addr

	async def __call__(self, scope, receive, send):
		if scope["type"] in ("http", "websocket"):
			scope = {**scope, "client": (self.addr, 50000)}
		await self.app(scope, receive, send)

	def __getattr__(self, name):
		return getattr(self.app, name)


class FakeUpstream:
	"""httpx.MockTransport handler: records every request and replies via a swappable responder.

	The responder may be sync or async, and may raise httpx errors to simulate failures.
	"""

	def __init__(self):
		self.requests: list[httpx.Request] = []
		self.responder = lambda request: httpx.Response(200, json={"ok": True})

	def __call__(self, request: httpx.Request):
		self.requests.append(request)
		return self.responder(request)


class FakePrompter:
	"""Answers approval prompts with .answer (a Decision, None, or an exception to raise).
	With .hold set, ask() waits for release(). That uses a threading.Event because
	TestClient runs the app's event loop on another thread."""

	def __init__(self):
		self.answer = Decision.SESSION
		self.messages: list[str] = []
		self.hold = False
		self._released = threading.Event()
		self.active = 0
		self.max_active = 0

	async def ask(self, message: str, timeout_s: float):
		self.messages.append(message)
		self.active += 1
		self.max_active = max(self.max_active, self.active)
		try:
			if self.hold and not await asyncio.to_thread(self._released.wait, timeout_s):
				return None
			if isinstance(self.answer, BaseException):
				raise self.answer
			return self.answer
		finally:
			self.active -= 1

	def release(self) -> None:
		self._released.set()


@pytest.fixture
def prompter():
	return FakePrompter()


@pytest.fixture
def settings(tmp_path):
	return Settings(
		db_path=tmp_path / "proxy.db",
		cache_dir=tmp_path / "_cache",
		upstreams_seed="jira=https://jira.test",
		allowed_hosts="localhost,127.0.0.1,[::1],testserver",
	)


@pytest.fixture
def fake_upstream():
	return FakeUpstream()


@pytest.fixture
def secrets():
	return MemorySecretStore()


@pytest.fixture
def proxy_secrets():
	return MemorySecretStore()


@pytest.fixture
def make_client(settings, fake_upstream, secrets, proxy_secrets, prompter, monkeypatch):
	"""Start the app (lifespan included) with optional Settings overrides.
	Requests carry X-WR-Upstream: jira unless wr_upstream says otherwise (None sends none).
	GET retries happen, but without their backoff sleeps (test_retries.py covers the delays)."""
	monkeypatch.setattr(upstream, "_backoff_s", lambda retry, rng: 0.0)
	stack = contextlib.ExitStack()

	def _make(client_addr: str = "127.0.0.1", wr_upstream: str | None = "jira", **overrides) -> TestClient:
		app = create_app(
			dataclasses.replace(settings, **overrides),
			transport=httpx.MockTransport(fake_upstream), secrets=secrets, prompter=prompter,
			proxy_secrets=proxy_secrets,
		)
		headers = {"X-WR-Upstream": wr_upstream} if wr_upstream else None
		return stack.enter_context(TestClient(ClientAddr(app, client_addr), headers=headers))

	yield _make
	stack.close()


@pytest.fixture
def client(make_client):
	return make_client()


@pytest.fixture
def admin_db(client, settings):
	"""A second connection to the running app's database, as an admin tool would hold."""
	conn = db.connect(settings.db_path)
	yield conn
	conn.close()


@pytest.fixture
def add_identity(settings, secrets):
	"""Create an identity (default: basic, 20 of 30 days left, bound to /jira) and its token, as the CLI would."""
	def _add(name="jira-uat", upstream_id="jira", token="tok-1", days_left=20, lifetime=30, **overrides):
		"""upstream_id: one upstream (or a list of them) the new identity is bound to; None for none."""
		now = datetime.now(timezone.utc)
		fields = dict(
			name=name, scheme="basic", username="ran@example.com",
			issued_at=now - timedelta(days=lifetime - days_left), expires_at=now + timedelta(days=days_left),
			rotate_url="https://id.atlassian.com/manage-profile/security/api-tokens",
		)
		fields.update(overrides)
		if token is not None:
			secrets.set(name, token)
		conn = db.connect(settings.db_path)
		try:
			db.create_identity(conn, **fields)
			for p in [] if upstream_id is None else [upstream_id] if isinstance(upstream_id, str) else upstream_id:
				db.set_upstream_identities(conn, p, [*db.identities_by_upstream(conn).get(p, []), name])
			return db.get_identity(conn, name)
		finally:
			conn.close()
	return _add


@pytest.fixture
def add_proxy(settings, proxy_secrets):
	"""Create an outbound proxy (default: corp-egress, http, user egress, a Keychain password) and route
	upstreams through it, as the admin UI would."""
	def _add(name="corp-egress", url="http://10.0.0.5:3128", username="egress", password="pw-1", route=("jira",), **overrides):
		if password is not None:
			proxy_secrets.set(name, password)
		conn = db.connect(settings.db_path)
		try:
			db.create_proxy(conn, db.OutboundProxy(name=name, url=url, username=username, **overrides))
			for upstream_id in route:
				db.put_upstream(conn, dataclasses.replace(db.get_upstream(conn, upstream_id), proxy=name))
			return db.get_proxy(conn, name)
		finally:
			conn.close()
	return _add
