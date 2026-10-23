# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Command-line management of identities and approvals, until the Plan 3 admin UI exists.

	python3 -m proxy identity add NAME --scheme basic|bearer|custom [--username U] [--header H]
		--expires WHEN [--issued WHEN] [--rotate-url URL] [--token-stdin]
	python3 -m proxy identity rotate NAME --expires WHEN [--rotate-url URL] [--token-stdin]
	python3 -m proxy identity list
	python3 -m proxy identity delete NAME [--yes]
	python3 -m proxy approval list
	python3 -m proxy approval revoke ADDRESS NAME

WHEN is a date (YYYY-MM-DD, read as 00:00 UTC), an ISO 8601 timestamp with an offset, or a
duration from now such as 30d or 12h. A new identity is bound to no upstream; bind it to
one or more on the admin Upstreams page. Tokens come from getpass, or from one line of stdin
with --token-stdin; never from argv or the environment. A custom identity's token is the whole
value of the header it names with --header (Authorization when not given, e.g. x-api-key or
Cookie). A running proxy picks changes up on its next request.
"""

import argparse
import asyncio
import getpass as getpass_module
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TextIO

from proxy import db
from proxy.config import Settings
from proxy.identities import (
	add_identity,
	health,
	parse_when,   # re-exported: cli.parse_when is the documented name
	remove_identity,
	rotate_token,
	stored_identity,
	validate_token,
)
from proxy.keychain import KeychainStore, SecretStore

__all__ = ["main", "parse_when", "battery"]


def battery(fraction: float, cells: int = 10) -> str:
	full = round(max(0.0, min(1.0, fraction)) * cells)
	return "█" * full + "░" * (cells - full)


@dataclass
class _Context:
	conn: sqlite3.Connection
	secrets_factory: Callable[[], SecretStore]
	stdin: TextIO
	stdout: TextIO
	stderr: TextIO
	getpass: Callable[[str], str]
	now: datetime
	_secrets: SecretStore | None = None

	@property
	def secrets(self) -> SecretStore:
		if self._secrets is None:
			self._secrets = self.secrets_factory()   # only commands that need the Keychain touch it
		return self._secrets

	def say(self, message: str) -> None:
		print(message, file=self.stdout)


def _read_token(args: argparse.Namespace, ctx: _Context, name: str, scheme: str, header: str | None) -> str:
	if scheme == "custom" and header == "Authorization":
		prompt = f"Authorization value for '{name}' (e.g. Token abc123): "
	elif scheme == "custom":
		prompt = f"{header} value for '{name}': "
	else:
		prompt = f"API token for '{name}': "
	raw = ctx.stdin.readline() if args.token_stdin else ctx.getpass(prompt)
	return validate_token(raw, scheme)


def _identity_add(args: argparse.Namespace, ctx: _Context) -> int:
	issued = parse_when(args.issued, ctx.now) if args.issued else ctx.now
	expires = parse_when(args.expires, ctx.now)
	fields = dict(
		name=args.name, scheme=args.scheme, username=args.username,
		header=args.header or ("Authorization" if args.scheme == "custom" else None),
		issued_at=issued, expires_at=expires, rotate_url=args.rotate_url,
	)
	db.check_identity(ctx.conn, **fields)   # before the token prompt (the helper checks again, before the Keychain)
	token = _read_token(args, ctx, args.name, args.scheme, fields["header"])
	identity = asyncio.run(add_identity(ctx.conn, ctx.secrets, **fields, token=token))
	ctx.say(
		f"added identity '{identity.name}'; token expires {identity.expires_at:%Y-%m-%d}; "
		"bind it to upstreams on the admin Upstreams page"
	)
	return 0


def _identity_rotate(args: argparse.Namespace, ctx: _Context) -> int:
	stored = stored_identity(ctx.conn, args.name)
	name = stored.name
	expires = parse_when(args.expires, ctx.now)
	db.check_rotation(issued_at=ctx.now, expires_at=expires, rotate_url=args.rotate_url)   # before the token prompt
	token = _read_token(args, ctx, name, stored.scheme, stored.header)
	identity = asyncio.run(rotate_token(
		ctx.conn, ctx.secrets, name, token=token, issued_at=ctx.now, expires_at=expires, rotate_url=args.rotate_url,
	))
	ctx.say(f"rotated identity '{identity.name}'; token expires {identity.expires_at:%Y-%m-%d}")
	return 0


def _identity_list(args: argparse.Namespace, ctx: _Context) -> int:
	identities = db.list_identities(ctx.conn)
	if not identities:
		ctx.say("no identities")
		return 0
	for identity in identities:
		h = health(identity, ctx.now)
		upstreams = ",".join(identity.upstream_ids) or "-"
		ctx.say(
			f"{identity.name:<20} {upstreams:<24} {identity.scheme:<6} {identity.username or identity.header or '-':<28} "
			f"{battery(h.fraction)} {h.fraction:>4.0%} {h.level:<8} "
			f"expires {identity.expires_at:%Y-%m-%d}  {identity.rotate_url or ''}".rstrip()
		)
	return 0


def _identity_delete(args: argparse.Namespace, ctx: _Context) -> int:
	name = stored_identity(ctx.conn, args.name).name
	if not args.yes:
		ctx.stdout.write(f"Delete identity '{name}', its Keychain token and its approvals? [y/N] ")
		ctx.stdout.flush()
		if ctx.stdin.readline().strip().lower() not in ("y", "yes"):
			ctx.say("not deleted")
			return 1
	_, had_item = asyncio.run(remove_identity(ctx.conn, ctx.secrets, name))   # a KeychainError becomes the error line
	if not had_item:
		ctx.say(f"note: there was no Keychain item for '{name}'")
	ctx.say(f"deleted identity '{name}'")
	return 0


def _approval_list(args: argparse.Namespace, ctx: _Context) -> int:
	for approval in db.list_approvals(ctx.conn):
		ctx.say(f"{approval.client_addr:<40} {approval.identity:<20} always, since {approval.granted_at:%Y-%m-%d %H:%M} UTC")
	ctx.say("(One hour and Session approvals live in the running proxy and are not listed here.)")
	return 0


def _approval_revoke(args: argparse.Namespace, ctx: _Context) -> int:
	if not db.delete_approval(ctx.conn, args.address, args.name):
		raise LookupError(f"no Always approval for {args.address} using {args.name!r}")
	ctx.say(f"revoked: {args.address} using '{args.name}' will be asked again")
	return 0


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="python3 -m proxy", description="Manage Well RESTed identities and approvals.")
	groups = parser.add_subparsers(dest="group", required=True)

	identity = groups.add_parser("identity", help="manage identities").add_subparsers(dest="command", required=True)
	add = identity.add_parser("add", help="add an identity and store its token in the Keychain")
	add.add_argument("name")
	add.add_argument("--scheme", required=True, choices=db.SCHEMES)
	add.add_argument("--username")
	add.add_argument("--header", help="custom only: the header the token is sent under (default Authorization)")
	add.add_argument("--expires", required=True)
	add.add_argument("--issued")
	add.add_argument("--rotate-url")
	add.add_argument("--token-stdin", action="store_true")
	add.set_defaults(handler=_identity_add)
	rotate = identity.add_parser("rotate", help="replace an identity's token")
	rotate.add_argument("name")
	rotate.add_argument("--expires", required=True)
	rotate.add_argument("--rotate-url")
	rotate.add_argument("--token-stdin", action="store_true")
	rotate.set_defaults(handler=_identity_rotate)
	identity.add_parser("list", help="list identities with token health").set_defaults(handler=_identity_list)
	delete = identity.add_parser("delete", help="delete an identity, its token and its approvals")
	delete.add_argument("name")
	delete.add_argument("--yes", action="store_true")
	delete.set_defaults(handler=_identity_delete)

	approval = groups.add_parser("approval", help="manage Always approvals").add_subparsers(dest="command", required=True)
	approval.add_parser("list", help="list Always approvals").set_defaults(handler=_approval_list)
	revoke = approval.add_parser("revoke", help="revoke an Always approval")
	revoke.add_argument("address")
	revoke.add_argument("name")
	revoke.set_defaults(handler=_approval_revoke)
	return parser


def main(
	argv: list[str],
	*,
	settings: Settings | None = None,
	secrets: SecretStore | None = None,
	stdin: TextIO | None = None,
	stdout: TextIO | None = None,
	stderr: TextIO | None = None,
	getpass: Callable[[str], str] = getpass_module.getpass,
	now: datetime | None = None,
) -> int:
	args = _parser().parse_args(argv)
	settings = settings or Settings.from_env()
	conn = db.connect(settings.db_path)
	ctx = _Context(
		conn=conn,
		secrets_factory=(lambda: secrets) if secrets is not None else KeychainStore,
		stdin=stdin or sys.stdin,
		stdout=stdout or sys.stdout,
		stderr=stderr or sys.stderr,
		getpass=getpass,
		now=now or datetime.now(timezone.utc),
	)
	try:
		return args.handler(args, ctx)
	except (ValueError, LookupError, RuntimeError) as exc:
		print(f"error: {exc}", file=ctx.stderr)
		return 1
	finally:
		conn.close()
