# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Entry point: python -m proxy runs the server (single worker by design: shelve + asyncio
locks); python -m proxy identity|approval … manages identities and approvals."""

import sys
from typing import Sequence

import uvicorn

from proxy.config import Settings
from proxy.main import create_app

_CLI_GROUPS = ("identity", "approval")


def main(argv: Sequence[str] = ()) -> None:
	if argv and argv[0] in _CLI_GROUPS:
		from proxy import cli
		raise SystemExit(cli.main(list(argv)))
	if argv:
		raise SystemExit(f"usage: python3 -m proxy [identity|approval ...]; unknown argument {argv[0]!r}")
	settings = Settings.from_env()
	uvicorn.run(
		create_app(settings), host=settings.host, port=settings.port, workers=1,
		server_header=False, date_header=False,
	)


if __name__ == "__main__":
	main(sys.argv[1:])
