# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Central logging for the proxy.

Every module gets a tagged logger: log = get_logger(__name__, "TAG"). Messages are
prefixed "[TAG] ". configure() sets the global level floor and an optional tag
allowlist, so noisy subsystems can be silenced from one place.

Never log raw auth header values; log derived identities only.
"""

import logging
import sys
from typing import TextIO

_ROOT = "proxy"


class _TagFilter(logging.Filter):
	"""Passes only records whose tag is in the allowlist (None = allow all)."""

	def __init__(self, enabled: set[str] | None):
		super().__init__()
		self.enabled = enabled

	def filter(self, record: logging.LogRecord) -> bool:
		if self.enabled is None:
			return True
		return getattr(record, "tag", None) in self.enabled


class TaggedLogger(logging.LoggerAdapter):
	"""Prefixes messages with [TAG] and attaches the tag to the record for filtering."""

	def process(self, msg, kwargs):
		tag = self.extra["tag"]
		kwargs["extra"] = {**kwargs.get("extra", {}), "tag": tag}
		return f"[{tag}] {msg}", kwargs


def get_logger(name: str, tag: str) -> TaggedLogger:
	"""Return a tagged logger; name should be the calling module's __name__."""
	return TaggedLogger(logging.getLogger(name), {"tag": tag})


def configure(level: str = "DEBUG", tags: str = "", stream: TextIO | None = None) -> None:
	"""(Re)configure the proxy logger tree. Safe to call more than once.

	level:  global floor (DEBUG, INFO, WARNING, ERROR).
	tags:   comma-separated allowlist, e.g. "CACHE,RULES"; empty means all tags.
	stream: output stream; defaults to stderr.
	"""
	root = logging.getLogger(_ROOT)
	root.setLevel(level.upper())
	for handler in list(root.handlers):
		root.removeHandler(handler)
	handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
	enabled = {t.strip().upper() for t in tags.split(",") if t.strip()} or None
	handler.addFilter(_TagFilter(enabled))
	root.addHandler(handler)
	root.propagate = False
