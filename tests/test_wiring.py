# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from proxy import db
from proxy.keychain import MemorySecretStore
from proxy.main import create_app


def test_the_loading_step_reads_every_token_once_at_startup(settings, secrets, prompter, fake_upstream):
	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	now = datetime.now(timezone.utc)
	for name in ("a", "b"):
		db.create_identity(conn, name=name, scheme="bearer", issued_at=now, expires_at=now + timedelta(days=9))
		secrets.set(name, f"tok-{name}")
	conn.close()

	app = create_app(settings, transport=httpx.MockTransport(fake_upstream), secrets=secrets, prompter=prompter, proxy_secrets=MemorySecretStore())
	with TestClient(app):
		assert secrets.reads == 2
		assert app.state.identities.resolve("a") is not None


def test_startup_survives_a_missing_token(settings, secrets, prompter, fake_upstream, capsys):
	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	now = datetime.now(timezone.utc)
	db.create_identity(conn, name="lost", scheme="bearer", issued_at=now, expires_at=now + timedelta(days=9))
	conn.close()

	app = create_app(settings, transport=httpx.MockTransport(fake_upstream), secrets=secrets, prompter=prompter, proxy_secrets=MemorySecretStore())
	with TestClient(app):
		assert "identity=lost" in capsys.readouterr().err
