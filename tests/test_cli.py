# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import io
from datetime import datetime, timedelta, timezone

import pytest

from proxy import cli, db
from proxy.keychain import MemorySecretStore

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(settings):
	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira-uat", "https://jira-uat.example.com"))
	conn.close()
	secrets = MemorySecretStore()
	out, err = io.StringIO(), io.StringIO()

	def run(*argv, stdin="", getpass=lambda prompt: pytest.fail("getpass not expected")):
		return cli.main(
			list(argv), settings=settings, secrets=secrets, stdin=io.StringIO(stdin),
			stdout=out, stderr=err, getpass=getpass, now=NOW,
		)

	def conn():
		return db.connect(settings.db_path)

	return run, secrets, out, err, conn


ADD = ("identity", "add", "jira-uat", "--scheme", "basic",
	"--username", "ran@example.com", "--expires", "30d", "--token-stdin",
	"--rotate-url", "https://id.atlassian.com/manage-profile/security/api-tokens")


@pytest.mark.parametrize("value, expected", [
	("30d", NOW + timedelta(days=30)),
	("12h", NOW + timedelta(hours=12)),
	("2026-11-09", datetime(2026, 11, 9, tzinfo=timezone.utc)),
	("2026-11-09T08:00:00-05:00", datetime(2026, 11, 9, 13, tzinfo=timezone.utc)),
])
def test_parse_when(value, expected):
	assert cli.parse_when(value, NOW) == expected


@pytest.mark.parametrize("value", ["soon", "2026-11-09T08:00:00", "30x"])
def test_parse_when_refuses(value):
	with pytest.raises(ValueError):
		cli.parse_when(value, NOW)


def test_add_stores_the_token_in_the_keychain_only(env):
	run, secrets, out, _, conn = env
	assert run(*ADD, stdin="tok-secret-1\n") == 0
	assert secrets.get("jira-uat") == "tok-secret-1"
	identity = db.get_identity(conn(), "jira-uat")
	assert identity.expires_at == NOW + timedelta(days=30)
	assert "tok-secret-1" not in out.getvalue()


def test_add_reads_the_token_with_getpass_by_default(env):
	run, secrets, *_ = env
	argv = [a for a in ADD if a != "--token-stdin"]
	assert run(*argv, getpass=lambda prompt: "tok-typed") == 0
	assert secrets.get("jira-uat") == "tok-typed"


CUSTOM = ("identity", "add", "proj", "--scheme", "custom", "--expires", "30d")


def test_add_custom_stores_the_whole_header_value(env):
	run, secrets, out, _, conn = env
	assert run(*CUSTOM, "--token-stdin", stdin="Token tok-secret-c1\n") == 0
	assert secrets.get("proj") == "Token tok-secret-c1"
	assert db.get_identity(conn(), "proj").scheme == "custom"
	assert "tok-secret-c1" not in out.getvalue()


def test_add_custom_prompts_for_the_header_value(env):
	run, secrets, *_ = env
	prompts = []

	def getpass(prompt):
		prompts.append(prompt)
		return "Token typed"

	assert run(*CUSTOM, getpass=getpass) == 0
	assert prompts == ["Authorization value for 'proj' (e.g. Token abc123): "]
	assert secrets.get("proj") == "Token typed"
	assert db.get_identity(env[4](), "proj").header == "Authorization"   # the default when --header is not given


def test_add_custom_with_a_header_name(env):
	run, secrets, out, _, conn = env
	prompts = []

	def getpass(prompt):
		prompts.append(prompt)
		return "sk-secret-1"

	assert run(*CUSTOM, "--header", "x-api-key", getpass=getpass) == 0
	assert prompts == ["x-api-key value for 'proj': "]
	assert secrets.get("proj") == "sk-secret-1"
	assert db.get_identity(conn(), "proj").header == "x-api-key"
	assert "sk-secret-1" not in out.getvalue()


@pytest.mark.parametrize("argv, fragment", [
	(("identity", "add", "svc", "--scheme", "bearer", "--header", "x-api-key", "--expires", "30d"), "takes no header name"),
	((*CUSTOM, "--header", "Host"), "header name"),
	((*CUSTOM, "--header", "bad name"), "header name"),
])
def test_bad_header_names_are_refused_before_asking_for_a_token(env, argv, fragment):
	run, secrets, _, err, conn = env
	assert run(*argv) == 1   # getpass would fail the test
	assert fragment in err.getvalue()
	assert secrets.get("svc") is None and secrets.get("proj") is None


def test_rotate_custom_asks_for_the_stored_headers_value(env):
	run, secrets, *_ = env
	run(*CUSTOM, "--header", "Cookie", "--token-stdin", stdin="session=one\n")
	prompts = []

	def getpass(prompt):
		prompts.append(prompt)
		return "session=two"

	assert run("identity", "rotate", "proj", "--expires", "30d", getpass=getpass) == 0
	assert prompts == ["Cookie value for 'proj': "]
	assert secrets.get("proj") == "session=two"


def test_list_shows_a_custom_identitys_header_name(env):
	run, _, out, _, _ = env
	run(*CUSTOM, "--header", "x-api-key", "--token-stdin", stdin="sk-secret-1\n")
	out.truncate(0)
	assert run("identity", "list") == 0
	assert "custom" in out.getvalue() and "x-api-key" in out.getvalue()
	assert "sk-secret-1" not in out.getvalue()


def test_add_custom_refuses_a_header_break(env):
	run, secrets, _, err, _ = env
	assert run(*CUSTOM, "--token-stdin", stdin="Token abc\rX-Evil: 1\n") != 0
	assert "header value" in err.getvalue()
	assert secrets.get("proj") is None


def test_rotate_custom_keeps_inner_spaces(env):
	run, secrets, *_ = env
	run(*CUSTOM, "--token-stdin", stdin="Token one\n")
	assert run("identity", "rotate", "proj", "--expires", "30d", "--token-stdin", stdin="Token two\n") == 0
	assert secrets.get("proj") == "Token two"


def test_bad_fields_are_refused_before_asking_for_a_token(env):
	run, secrets, _, err, _ = env
	argv = [a if a != "jira-uat" else "anonymous" for a in ADD]   # a reserved name
	assert run(*[a for a in argv if a != "--token-stdin"]) == 1   # getpass would fail the test
	assert "reserved" in err.getvalue()
	assert secrets.get("anonymous") is None


def test_add_creates_an_unbound_identity_and_says_where_to_bind_it(env):
	run, _, out, _, conn = env
	assert run(*ADD, stdin="tok-1\n") == 0
	assert db.get_identity(conn(), "jira-uat").upstream_ids == ()
	assert "Upstreams page" in out.getvalue()


def test_add_no_longer_takes_a_prefix(env):
	run, *_ = env
	with pytest.raises(SystemExit):
		run(*ADD, "--upstream_id", "jira-uat", stdin="tok-1\n")


def test_list_shows_bound_upstreams(env):
	run, _, out, _, conn = env
	run(*ADD, stdin="tok-1\n")
	c = conn()
	db.put_upstream(c, db.Upstream("conf-uat", "https://conf-uat.test"))
	db.set_upstream_identities(c, "jira-uat", ["jira-uat"])
	db.set_upstream_identities(c, "conf-uat", ["jira-uat"])
	out.truncate(0)
	run("identity", "list")
	assert "conf-uat,jira-uat" in out.getvalue()
	run("identity", "add", "bare", "--scheme", "bearer", "--expires", "30d", "--token-stdin", stdin="tok-2\n")
	out.seek(0)
	out.truncate(0)
	run("identity", "list")
	[bare] = [line for line in out.getvalue().splitlines() if line.startswith("bare")]
	assert bare.split()[1] == "-"


def test_duplicate_add_leaves_the_existing_token_alone(env):
	run, secrets, _, err, _ = env
	run(*ADD, stdin="tok-1\n")
	assert run(*ADD, stdin="tok-2\n") == 1
	assert secrets.get("jira-uat") == "tok-1"
	assert "already exists" in err.getvalue()


def test_rotate_replaces_the_token_and_keeps_the_identity(env):
	run, secrets, _, _, conn = env
	run(*ADD, stdin="tok-1\n")
	before = db.get_identity(conn(), "jira-uat")
	assert run("identity", "rotate", "jira-uat", "--expires", "2026-12-01", "--token-stdin", stdin="tok-2\n") == 0
	after = db.get_identity(conn(), "jira-uat")
	assert secrets.get("jira-uat") == "tok-2"
	assert after.id == before.id
	assert after.expires_at == datetime(2026, 12, 1, tzinfo=timezone.utc)


def test_list_shows_health_without_tokens(env):
	run, _, out, _, _ = env
	run(*ADD, stdin="tok-secret-1\n")
	out.truncate(0)
	assert run("identity", "list") == 0
	line = out.getvalue()
	assert "jira-uat" in line and "██████████" in line and "green" in line
	assert "tok-secret-1" not in line


def test_delete_asks_first(env):
	run, secrets, _, _, conn = env
	run(*ADD, stdin="tok-1\n")
	assert run("identity", "delete", "jira-uat", stdin="n\n") == 1
	assert db.get_identity(conn(), "jira-uat") is not None
	assert run("identity", "delete", "jira-uat", stdin="y\n") == 0
	assert db.get_identity(conn(), "jira-uat") is None
	assert secrets.get("jira-uat") is None


def test_approvals_list_and_revoke(env):
	run, _, out, _, conn = env
	run(*ADD, stdin="tok-1\n")
	c = conn()
	db.put_approval(c, "192.0.2.111", db.get_identity(c, "jira-uat").id)
	assert run("approval", "list") == 0
	assert "192.0.2.111" in out.getvalue()
	assert run("approval", "revoke", "192.0.2.111", "jira-uat") == 0
	assert run("approval", "revoke", "192.0.2.111", "jira-uat") == 1


def test_battery():
	assert cli.battery(1.0) == "██████████"
	assert cli.battery(0.34) == "███░░░░░░░"
	assert cli.battery(0.0) == "░░░░░░░░░░"


def test_rotate_uses_the_stored_name_casing(env):
	run, secrets, out, _, conn = env
	run(*ADD, stdin="tok-1\n")
	out.truncate(0)
	assert run("identity", "rotate", "Jira-UAT", "--expires", "30d", "--token-stdin", stdin="tok-2\n") == 0
	assert secrets.get("jira-uat") == "tok-2"
	assert secrets.get("Jira-UAT") is None
	assert "'jira-uat'" in out.getvalue()
	assert db.get_identity(conn(), "jira-uat").name == "jira-uat"


def test_delete_uses_the_stored_name_casing(env):
	run, secrets, out, _, conn = env
	run(*ADD, stdin="tok-1\n")
	out.truncate(0)
	assert run("identity", "delete", "Jira-UAT", "--yes") == 0
	assert secrets.get("jira-uat") is None
	assert db.get_identity(conn(), "jira-uat") is None
	assert "no Keychain item" not in out.getvalue()
	assert "'jira-uat'" in out.getvalue()


def test_keychain_failure_is_an_error_line_not_a_traceback(settings, capsys):
	from proxy.keychain import KeychainError

	class LockedStore(MemorySecretStore):
		def set(self, name, token):
			raise KeychainError(f"Keychain write failed for '{name}': KeyringLocked: Keychain Access Denied")

	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira-uat", "https://jira-uat.example.com"))
	conn.close()
	err = io.StringIO()
	code = cli.main(list(ADD), settings=settings, secrets=LockedStore(), stdin=io.StringIO("tok\n"), stdout=io.StringIO(), stderr=err, now=NOW)
	assert code == 1
	assert err.getvalue().startswith("error: Keychain write failed for 'jira-uat': KeyringLocked")
	assert db.get_identity(db.connect(settings.db_path), "jira-uat") is None


# --- identity delete must not strand a Keychain token -------------------------

def test_delete_stops_when_the_keychain_refuses(env):
	from proxy.keychain import KeychainError
	run, secrets, out, err, conn = env
	run(*ADD, stdin="tok-1\n")
	c = conn()
	db.put_approval(c, "192.0.2.111", db.get_identity(c, "jira-uat").id)
	out.truncate(0)

	def locked_delete(name):
		return False

	def locked_get(name):
		raise KeychainError(f"Keychain read failed for '{name}': KeyringLocked: Keychain Access Denied")

	secrets.delete, secrets.get = locked_delete, locked_get
	assert run("identity", "delete", "jira-uat", "--yes") == 1
	assert err.getvalue().startswith("error: Keychain read failed")
	assert db.get_identity(c, "jira-uat") is not None
	assert len(db.list_approvals(c)) == 1
	assert "deleted identity" not in out.getvalue()


def test_delete_stops_when_the_item_survives_the_delete(env):
	run, secrets, out, err, conn = env
	run(*ADD, stdin="tok-1\n")
	out.truncate(0)
	secrets.delete = lambda name: False
	assert run("identity", "delete", "jira-uat", "--yes") == 1
	assert "could not delete the Keychain item for 'jira-uat'; nothing was deleted" in err.getvalue()
	assert db.get_identity(conn(), "jira-uat") is not None
	assert secrets.get("jira-uat") == "tok-1"
	assert "deleted identity" not in out.getvalue()


def test_delete_carries_on_when_the_item_is_truly_absent(env):
	run, secrets, out, _, conn = env
	run(*ADD, stdin="tok-1\n")
	out.truncate(0)
	assert MemorySecretStore.delete(secrets, "jira-uat")   # the item vanished before the command ran
	assert run("identity", "delete", "jira-uat", "--yes") == 0
	assert "note: there was no Keychain item for 'jira-uat'" in out.getvalue()
	assert "deleted identity 'jira-uat'" in out.getvalue()
	assert db.get_identity(conn(), "jira-uat") is None
