# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

import proxy.__main__ as entry


def test_main_runs_uvicorn_single_worker_without_duplicate_headers(monkeypatch, tmp_path):
	monkeypatch.setenv("PROXY_DB_PATH", str(tmp_path / "proxy.db"))
	monkeypatch.setenv("PROXY_CACHE_DIR", str(tmp_path / "_cache"))
	calls = []
	monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
	entry.main()
	assert len(calls) == 1
	kwargs = calls[0]
	assert kwargs["workers"] == 1
	assert kwargs["server_header"] is False
	assert kwargs["date_header"] is False
	assert kwargs["host"] == "127.0.0.1"


def test_identity_and_approval_arguments_dispatch_to_the_cli(monkeypatch):
	import proxy.cli as cli
	seen = []
	monkeypatch.setattr(cli, "main", lambda argv, **kwargs: seen.append(argv) or 0)
	monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: pytest.fail("the server must not start"))
	with pytest.raises(SystemExit) as exc:
		entry.main(["identity", "list"])
	assert exc.value.code == 0
	assert seen == [["identity", "list"]]


def test_unknown_argument_exits_with_usage(monkeypatch):
	monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: pytest.fail("the server must not start"))
	with pytest.raises(SystemExit) as exc:
		entry.main(["serve"])
	assert "usage" in str(exc.value.code)
