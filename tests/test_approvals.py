# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from proxy import db
from proxy.approvals import ApprovalGate, Decision, OsascriptPrompter, Verdict, prompt_message

LAN = "192.0.2.111"
NOW = datetime.now(timezone.utc)


class Clock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self) -> float:
		return self.now


@pytest.fixture
def conn(tmp_path):
	conn = db.connect(tmp_path / "p.db")
	db.put_upstream(conn, db.Upstream("jira-uat", "https://jira-uat.example.com"))
	yield conn
	conn.close()


@pytest.fixture
def upstream(conn):
	return db.get_upstream(conn, "jira-uat")


def make_identity(conn, name="jira-uat"):
	return db.create_identity(
		conn, name=name, scheme="bearer",
		issued_at=NOW, expires_at=NOW + timedelta(days=30),
	)


@pytest.fixture
def identity(conn):
	return make_identity(conn)


@pytest.fixture
def clock():
	return Clock()


@pytest.fixture
def gate(conn, prompter, clock):
	gate = ApprovalGate(conn, prompter, wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	yield gate


def test_prompt_message(identity, upstream):
	assert prompt_message(LAN, identity, upstream) == (
		'192.0.2.111 is trying to connect to https://jira-uat.example.com using the "jira-uat" identity. Allow?'
	)


async def test_loopback_is_allowed_without_asking(gate, prompter, identity, upstream):
	assert await gate.check("127.0.0.1", identity, upstream) is Verdict.ALLOWED
	assert await gate.check("::1", identity, upstream) is Verdict.ALLOWED
	assert prompter.messages == []


async def test_session_asks_once(gate, prompter, identity, upstream):
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(prompter.messages) == 1


async def test_one_hour_expires(gate, prompter, identity, upstream, clock):
	prompter.answer = Decision.HOUR
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	clock.now += 3599
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	clock.now += 2
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(prompter.messages) == 2


async def test_always_survives_a_restart(conn, gate, prompter, identity, upstream, clock):
	prompter.answer = Decision.ALWAYS
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert db.has_approval(conn, LAN, identity.id)
	fresh = ApprovalGate(conn, prompter, wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	assert await fresh.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(prompter.messages) == 1


async def test_revoking_always_asks_again(conn, gate, prompter, identity, upstream):
	prompter.answer = Decision.ALWAYS
	await gate.check(LAN, identity, upstream)
	db.delete_approval(conn, LAN, "jira-uat")
	prompter.answer = Decision.NO
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED
	assert len(prompter.messages) == 2


async def test_no_has_a_cooldown(gate, prompter, identity, upstream, clock):
	prompter.answer = Decision.NO
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED
	clock.now += 299
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED
	assert len(prompter.messages) == 1
	clock.now += 2
	prompter.answer = Decision.SESSION
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(prompter.messages) == 2


@pytest.mark.parametrize("answer", [None, RuntimeError("osascript exploded")])
async def test_no_answer_denies_this_request_and_records_nothing(conn, gate, prompter, identity, upstream, answer):
	prompter.answer = answer
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED
	prompter.answer = Decision.SESSION
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED   # asked again, no cooldown
	assert len(prompter.messages) == 2
	assert not db.has_approval(conn, LAN, identity.id)


async def test_concurrent_requests_share_one_prompt(gate, prompter, identity, upstream):
	prompter.hold = True
	checks = [asyncio.create_task(gate.check(LAN, identity, upstream)) for _ in range(5)]
	await asyncio.sleep(0.05)
	prompter.release()
	assert await asyncio.gather(*checks) == [Verdict.ALLOWED] * 5
	assert len(prompter.messages) == 1


async def test_slow_answer_is_pending_then_applies_to_the_retry(conn, prompter, identity, upstream, clock):
	gate = ApprovalGate(conn, prompter, wait_s=0.05, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	prompter.hold = True
	assert await gate.check(LAN, identity, upstream) is Verdict.PENDING
	prompter.release()
	await asyncio.sleep(0.1)
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(prompter.messages) == 1


async def test_prompts_are_shown_one_at_a_time(conn, gate, prompter, identity, upstream):
	other = make_identity(conn, "jira-prod")
	prompter.hold = True
	first = asyncio.create_task(gate.check(LAN, identity, upstream))
	second = asyncio.create_task(gate.check(LAN, other, upstream))
	await asyncio.sleep(0.05)
	assert len(prompter.messages) == 1
	prompter.release()
	assert await asyncio.gather(first, second) == [Verdict.ALLOWED, Verdict.ALLOWED]
	assert prompter.max_active == 1


async def test_recreated_identity_is_asked_about_afresh(conn, gate, prompter, identity, upstream):
	await gate.check(LAN, identity, upstream)
	db.delete_identity(conn, "jira-uat")
	recreated = make_identity(conn)
	await gate.check(LAN, recreated, upstream)
	assert len(prompter.messages) == 2


# --- answering from the approvals page: first answer wins, the loser is cleaned up ---

class NeverAnswers:
	"""A dialog that stays open until cancelled, and remembers that it was."""

	def __init__(self):
		self.cancelled = False
		self.messages: list[str] = []

	async def ask(self, message: str, timeout_s: float):
		self.messages.append(message)
		try:
			await asyncio.Event().wait()
		except asyncio.CancelledError:
			self.cancelled = True
			raise


async def parked(gate, identity, upstream):
	check = asyncio.create_task(gate.check(LAN, identity, upstream))
	await asyncio.sleep(0.02)
	assert not check.done()
	return check


async def test_ui_answer_resolves_a_waiting_request(conn, identity, upstream, clock):
	dialog = NeverAnswers()
	gate = ApprovalGate(conn, dialog, wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	check = await parked(gate, identity, upstream)
	assert gate.snapshot()["pending"] == [
		{"client_addr": LAN, "identity_id": identity.id, "message": prompt_message(LAN, identity, upstream), "asked_at": pytest.approx(time.time(), abs=5)},
	]
	assert gate.answer(LAN, identity.id, Decision.HOUR) is True
	assert await check is Verdict.ALLOWED
	assert dialog.cancelled is True
	snapshot = gate.snapshot()
	assert snapshot["pending"] == []
	assert snapshot["grants"] == [{"client_addr": LAN, "identity_id": identity.id, "kind": "hour", "expires_in_s": 3600}]
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert len(dialog.messages) == 1


async def test_dialog_first_ui_ignored(gate, prompter, identity, upstream):
	prompter.answer = Decision.SESSION
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED
	assert gate.answer(LAN, identity.id, Decision.NO) is False
	snapshot = gate.snapshot()
	assert snapshot["grants"] == [{"client_addr": LAN, "identity_id": identity.id, "kind": "session", "expires_in_s": None}]
	assert snapshot["cooldowns"] == [] and snapshot["pending"] == []


async def test_answer_no_sets_cooldown(conn, identity, upstream, clock):
	gate = ApprovalGate(conn, NeverAnswers(), wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	check = await parked(gate, identity, upstream)
	assert gate.answer(LAN, identity.id, Decision.NO) is True
	assert await check is Verdict.DENIED
	assert gate.snapshot()["cooldowns"] == [{"client_addr": LAN, "identity_id": identity.id, "remaining_s": 300}]
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED


async def test_answer_always_persists(conn, identity, upstream, clock):
	gate = ApprovalGate(conn, NeverAnswers(), wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	check = await parked(gate, identity, upstream)
	assert gate.answer(LAN, identity.id, Decision.ALWAYS) is True
	assert await check is Verdict.ALLOWED
	assert db.has_approval(conn, LAN, identity.id)


async def test_answer_without_pending_is_false(gate, identity):
	assert gate.answer(LAN, identity.id, Decision.HOUR) is False
	assert gate.answer("10.0.0.9", 999, Decision.HOUR) is False


async def test_snapshot_lists_all_three_kinds(conn, prompter, upstream, clock):
	hour, session, no = make_identity(conn, "hour"), make_identity(conn, "session"), make_identity(conn, "no")
	gate = ApprovalGate(conn, prompter, wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	for identity, decision in ((hour, Decision.HOUR), (session, Decision.SESSION), (no, Decision.NO)):
		prompter.answer = decision
		await gate.check(LAN, identity, upstream)
	clock.now += 100
	pending_identity = make_identity(conn, "pending")
	gate._prompter = NeverAnswers()
	check = await parked(gate, pending_identity, upstream)
	snapshot = gate.snapshot()
	assert [p["identity_id"] for p in snapshot["pending"]] == [pending_identity.id]
	assert {(g["identity_id"], g["kind"], g["expires_in_s"]) for g in snapshot["grants"]} == {(hour.id, "hour", 3500), (session.id, "session", None)}
	assert snapshot["cooldowns"] == [{"client_addr": LAN, "identity_id": no.id, "remaining_s": 200}]
	check.cancel()
	await asyncio.gather(check, return_exceptions=True)
	await gate.aclose()


async def test_revoke_grant_and_clear_cooldown(gate, prompter, identity, upstream, conn):
	prompter.answer = Decision.SESSION
	await gate.check(LAN, identity, upstream)
	assert gate.revoke_grant(LAN, identity.id) is True
	assert gate.revoke_grant(LAN, identity.id) is False
	prompter.answer = Decision.NO
	assert await gate.check(LAN, identity, upstream) is Verdict.DENIED   # asked again
	assert len(prompter.messages) == 2
	assert gate.clear_cooldown(LAN, identity.id) is True
	assert gate.clear_cooldown(LAN, identity.id) is False
	prompter.answer = Decision.SESSION
	assert await gate.check(LAN, identity, upstream) is Verdict.ALLOWED   # asked a third time, no cooldown
	assert len(prompter.messages) == 3


async def test_aclose_cancels_a_pending_prompt_without_recording(conn, identity, upstream, clock):
	dialog = NeverAnswers()
	gate = ApprovalGate(conn, dialog, wait_s=5.0, prompt_timeout_s=5.0, cooldown_s=300.0, clock=clock)
	check = await parked(gate, identity, upstream)
	await gate.aclose()
	await asyncio.gather(check, return_exceptions=True)
	assert dialog.cancelled is True
	assert gate.snapshot() == {"pending": [], "grants": [], "cooldowns": []}


# --- OsascriptPrompter, against a fake osascript on PATH ---

@pytest.fixture
def fake_osascript(tmp_path, monkeypatch):
	def install(body: str) -> None:
		script = tmp_path / "osascript"
		script.write_text(f"#!/bin/sh\n{body}\n")
		script.chmod(0o755)
		monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
	return install


async def test_message_travels_as_argv_not_as_script(fake_osascript, tmp_path):
	fake_osascript(f'printf "%s\\n" "$@" > "{tmp_path}/argv"; echo "One hour"')
	hostile = '1.2.3.4" & (do shell script "touch /tmp/pwned") & "'
	assert await OsascriptPrompter().ask(hostile, 5.0) is Decision.HOUR
	argv = (tmp_path / "argv").read_text().splitlines()
	assert argv[-1] == hostile
	assert all(hostile not in line for line in argv[:-1])


@pytest.mark.parametrize("body, expected", [
	('echo "Always"', Decision.ALWAYS),
	('echo "No"', Decision.NO),
	('echo "maybe"', None),
	("exit 1", None),
])
async def test_answers(fake_osascript, body, expected):
	fake_osascript(body)
	assert await OsascriptPrompter().ask("m", 5.0) is expected


async def test_an_unanswered_dialog_is_closed(fake_osascript):
	fake_osascript("sleep 10")
	started = time.monotonic()
	assert await OsascriptPrompter().ask("m", 0.2) is None
	assert time.monotonic() - started < 2.0
