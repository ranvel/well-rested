# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Client approvals: which non-loopback client may use which identity.

Little Snitch style: the first time a LAN address uses an identity, whoever is at the Mac
is asked. Every request for the same (address, identity) waits on that one prompt, and
prompts are shown one at a time. Always approvals live in SQLite and are read on each
request, so a CLI revoke is immediate. One hour, Session and the No cooldown live in this
process only. Keys use the identity's id, so a re-created identity is asked about afresh.

"Always" means "don't ask me again", not "access forever": the token's own expiry still
bounds it (see docs/cache-proxy-design.md, "Network trust and client approval").
"""

import asyncio
import enum
import os
import signal
import sqlite3
import time
from typing import Callable, Protocol

from proxy import db
from proxy.admission import is_loopback
from proxy.log import get_logger

log = get_logger(__name__, "APPRV")


class Decision(enum.Enum):
	ALWAYS = "Always"
	HOUR = "One hour"
	SESSION = "Session"
	NO = "No"


class Verdict(enum.Enum):
	ALLOWED = "allowed"
	DENIED = "denied"
	PENDING = "pending"


class Prompter(Protocol):
	async def ask(self, message: str, timeout_s: float) -> Decision | None:
		"""Show the prompt and return the choice, or None for no answer (timed out or failed)."""


def prompt_message(client_addr: str, identity: db.Identity, upstream: db.Upstream) -> str:
	return f'{client_addr} is trying to connect to {upstream.base_url} using the "{identity.name}" identity. Allow?'


_Key = tuple[str, int]   # (client address, identity id)
_ABSENT = object()


class ApprovalGate:
	def __init__(
		self,
		conn: sqlite3.Connection,
		prompter: Prompter,
		*,
		wait_s: float,
		prompt_timeout_s: float,
		cooldown_s: float,
		hour_s: float = 3600.0,
		clock: Callable[[], float] = time.monotonic,
	):
		self._conn = conn
		self._prompter = prompter
		self._wait_s = wait_s
		self._prompt_timeout_s = prompt_timeout_s
		self._cooldown_s = cooldown_s
		self._hour_s = hour_s
		self._clock = clock
		self._grants: dict[_Key, float | None] = {}   # expiry on the clock, or None for the session
		self._cooldowns: dict[_Key, float] = {}
		self._pending: dict[_Key, asyncio.Task] = {}
		self._answers: dict[_Key, asyncio.Future] = {}         # UI answers for pending prompts
		self._pending_info: dict[_Key, tuple[str, float]] = {}  # key → (message, asked_at wall-clock)
		self._screen = asyncio.Lock()                  # one dialog on screen at a time

	async def check(self, client_addr: str, identity: db.Identity, upstream: db.Upstream) -> Verdict:
		if is_loopback(client_addr):
			return Verdict.ALLOWED
		key = (client_addr, identity.id)
		now = self._clock()
		if self._granted(key, now) or db.has_approval(self._conn, client_addr, identity.id):
			return Verdict.ALLOWED
		until = self._cooldowns.get(key)
		if until is not None:
			if now < until:
				return Verdict.DENIED
			del self._cooldowns[key]
		task = self._pending.get(key)
		if task is None:
			message = prompt_message(client_addr, identity, upstream)
			task = self._pending[key] = asyncio.create_task(self._prompt(key, message))
		try:
			decision = await asyncio.wait_for(asyncio.shield(task), self._wait_s)
		except asyncio.TimeoutError:
			return Verdict.PENDING
		if decision in (Decision.ALWAYS, Decision.HOUR, Decision.SESSION):
			return Verdict.ALLOWED
		return Verdict.DENIED

	def _granted(self, key: _Key, now: float) -> bool:
		if key not in self._grants:
			return False
		expiry = self._grants[key]
		if expiry is None or now < expiry:
			return True
		del self._grants[key]
		return False

	async def _prompt(self, key: _Key, message: str) -> Decision | None:
		"""Race the on-screen dialog against an answer from the approvals page: first answer wins.

		A page answer cancels the dialog task, which kills its process group, so the dialog
		actually closes. A dialog answer simply leaves the page's future unused. Either way
		_record runs exactly once.
		"""
		ui: asyncio.Future = asyncio.get_running_loop().create_future()
		self._answers[key] = ui
		self._pending_info[key] = (message, time.time())
		on_screen = asyncio.create_task(self._ask_on_screen(message))
		try:
			done, _ = await asyncio.wait({on_screen, ui}, return_when=asyncio.FIRST_COMPLETED)
			if ui in done:
				on_screen.cancel()
				await asyncio.gather(on_screen, return_exceptions=True)
				decision = ui.result()
				log.info("approval answered on the admin page; client=%s identity_id=%d", *key)
			else:
				decision = on_screen.result()
		except asyncio.CancelledError:
			on_screen.cancel()
			await asyncio.gather(on_screen, return_exceptions=True)
			raise
		finally:
			self._answers.pop(key, None)
			self._pending_info.pop(key, None)
			self._pending.pop(key, None)
		self._record(key, decision)
		return decision

	async def _ask_on_screen(self, message: str) -> Decision | None:
		try:
			async with self._screen:
				return await self._prompter.ask(message, self._prompt_timeout_s)
		except asyncio.CancelledError:
			raise
		except Exception as exc:
			log.warning("approval prompt failed; error=%s", type(exc).__name__)
			return None

	def answer(self, client_addr: str, identity_id: int, decision: Decision) -> bool:
		"""Resolve a pending prompt from the approvals page. False when nothing is pending."""
		key = (client_addr, identity_id)
		future = self._answers.get(key)
		if future is None or future.done():
			return False
		future.set_result(decision)
		self._answers.pop(key, None)        # no longer pending from this moment, before the
		self._pending_info.pop(key, None)   # prompt task gets the loop back to finish and record
		return True

	def snapshot(self) -> dict:
		"""What the approvals page shows besides SQLite's Always rows. Ids, not names;
		the admin payload joins names through db.list_identities."""
		now = self._clock()
		return {
			"pending": [
				{"client_addr": addr, "identity_id": identity_id, "message": message, "asked_at": asked_at}
				for (addr, identity_id), (message, asked_at) in self._pending_info.items()
			],
			"grants": [
				{
					"client_addr": addr, "identity_id": identity_id,
					"kind": "session" if expiry is None else "hour",
					"expires_in_s": None if expiry is None else max(0, int(expiry - now)),
				}
				for (addr, identity_id), expiry in self._grants.items()
			],
			"cooldowns": [
				{"client_addr": addr, "identity_id": identity_id, "remaining_s": max(0, int(until - now))}
				for (addr, identity_id), until in self._cooldowns.items()
			],
		}

	def revoke_grant(self, client_addr: str, identity_id: int) -> bool:
		return self._grants.pop((client_addr, identity_id), _ABSENT) is not _ABSENT   # a Session grant is stored as None

	def clear_cooldown(self, client_addr: str, identity_id: int) -> bool:
		return self._cooldowns.pop((client_addr, identity_id), None) is not None

	def _record(self, key: _Key, decision: Decision | None) -> None:
		client_addr, identity_id = key
		if decision is Decision.ALWAYS:
			try:
				db.put_approval(self._conn, client_addr, identity_id)
			except sqlite3.IntegrityError:
				log.info("identity vanished during its prompt; identity_id=%d", identity_id)
		elif decision is Decision.HOUR:
			self._grants[key] = self._clock() + self._hour_s
		elif decision is Decision.SESSION:
			self._grants[key] = None
		elif decision is Decision.NO:
			self._cooldowns[key] = self._clock() + self._cooldown_s
		log.info(
			"approval decided; client=%s identity_id=%d decision=%s",
			client_addr, identity_id, decision.value if decision else "none",
		)

	async def aclose(self) -> None:
		for task in list(self._pending.values()):
			task.cancel()
		await asyncio.gather(*self._pending.values(), return_exceptions=True)
		self._pending.clear()


# AppleScript, one statement per -e. The message arrives as argv, never interpolated.
_SCRIPT = (
	"on run argv",
	"activate",
	'set picked to choose from list {"Always", "One hour", "Session", "No"} '
	'with title "Well RESTed" with prompt (item 1 of argv) default items {"No"} '
	'OK button name "Choose" cancel button name "Cancel"',
	'if picked is false then return "No"',
	"return item 1 of picked",
	"end run",
)


class OsascriptPrompter:
	"""A native macOS dialog. Needs the proxy to run in a GUI login session."""

	async def ask(self, message: str, timeout_s: float) -> Decision | None:
		args = [part for line in _SCRIPT for part in ("-e", line)]
		# Its own process group, so an unanswered dialog is torn down with any helper it spawned
		# (a surviving child would keep the pipes open and stall the wait below).
		proc = await asyncio.create_subprocess_exec(
			"osascript", *args, message,
			stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
		)
		try:
			out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
		except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
			try:
				os.killpg(proc.pid, signal.SIGKILL)
			except ProcessLookupError:
				pass
			await proc.wait()
			if isinstance(exc, asyncio.CancelledError):
				raise
			log.info("approval prompt unanswered; closed after %.0fs", timeout_s)
			return None
		if proc.returncode != 0:
			log.warning("osascript failed; returncode=%d", proc.returncode)
			return None
		try:
			return Decision(out.decode("utf-8", "replace").strip())
		except ValueError:
			return None
