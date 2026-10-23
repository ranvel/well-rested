# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cache-rule patterns: wildcard compilation and most-specific-wins matching.

Every rule belongs to one upstream and only ever matches that upstream's requests. Its
pattern matches the full request path, e.g. /rest/api/2/issue/ABC-123:
  *    any run of characters within one path segment (never crosses "/")
  **   any run of characters across segments
  /**  when followed by "/" or the end, may also match nothing, so /rest/**
       matches /rest itself as well as everything beneath it

Most specific wins, by: most literal characters, then fewest wildcards, then
longest pattern, then newest rule (highest id).
"""

import re
from dataclasses import dataclass
from typing import Iterable

from proxy.log import get_logger

log = get_logger(__name__, "RULES")

QUERY_MODES = ("include", "ignore", "reject")

_TOKEN = re.compile(r"(/\*\*(?=/|$)|\*\*|\*)")
_WILDCARD = re.compile(r"\*\*|\*")


@dataclass(frozen=True)
class Rule:
	id: int
	upstream_id: str                 # the only upstream this rule matches
	pattern: str
	ttl_seconds: int | None          # None = never expires; 0 = bypass (never cache)
	query_mode: str = "include"
	enabled: bool = True
	note: str | None = None
	created_at: str = ""
	updated_at: str = ""

	@property
	def bypass(self) -> bool:
		return self.ttl_seconds == 0


def compile_pattern(pattern: str) -> re.Pattern[str]:
	"""Compile a wildcard pattern; match with .fullmatch(). Raises ValueError if malformed."""
	if not pattern.startswith("/"):
		raise ValueError(f"pattern must start with '/': {pattern!r}")
	parts = []
	for token in _TOKEN.split(pattern):
		if token == "/**":
			parts.append("(?:/.*)?")
		elif token == "**":
			parts.append(".*")
		elif token == "*":
			parts.append("[^/]*")
		elif token:
			parts.append(re.escape(token))
	return re.compile("".join(parts), re.DOTALL)


def specificity(pattern: str) -> tuple[int, int]:
	"""(literal character count, wildcard count) for most-specific-wins ordering."""
	return len(pattern.replace("*", "")), len(_WILDCARD.findall(pattern))


@dataclass(frozen=True)
class _Compiled:
	rule: Rule
	regex: re.Pattern[str]
	literals: int
	wildcards: int


class RuleSet:
	"""Enabled rules, compiled once, grouped by upstream and ordered most-specific first."""

	def __init__(self, rules: Iterable[Rule]):
		compiled: dict[str, list[_Compiled]] = {}
		for rule in rules:
			if not rule.enabled:
				continue
			literals, wildcards = specificity(rule.pattern)
			compiled.setdefault(rule.upstream_id, []).append(
				_Compiled(rule, compile_pattern(rule.pattern), literals, wildcards),
			)
		for group in compiled.values():
			group.sort(key=lambda c: (-c.literals, c.wildcards, -len(c.rule.pattern), -c.rule.id))
		self._compiled = compiled
		log.debug("ruleset compiled; rules=%d upstreams=%d", len(self), len(compiled))

	def __len__(self) -> int:
		return sum(len(group) for group in self._compiled.values())

	def match(self, upstream_id: str, path: str) -> Rule | None:
		"""The most specific enabled rule of this upstream matching path, or None."""
		for c in self._compiled.get(upstream_id, ()):
			if c.regex.fullmatch(path):
				return c.rule
		return None

	def match_all(self, upstream_id: str, path: str) -> list[Rule]:
		"""Every enabled rule of this upstream matching path, most-specific first. match() is match_all()[0]."""
		return [c.rule for c in self._compiled.get(upstream_id, ()) if c.regex.fullmatch(path)]
