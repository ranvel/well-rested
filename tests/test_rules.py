# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import pytest

from proxy.rules import Rule, RuleSet, compile_pattern, specificity


def rule(id, pattern, ttl_seconds=60, upstream_id="jira", **kw):
	return Rule(id=id, upstream_id=upstream_id, pattern=pattern, ttl_seconds=ttl_seconds, **kw)


@pytest.mark.parametrize("pattern,path,expected", [
	("/jira/**", "/jira/rest/api/2/issue/ABC-123", True),
	("/jira/**", "/jira", True),
	("/jira/**", "/jira/", True),
	("/jira/**", "/jirafoo/x", False),
	("/jira/rest/api/2/issue/ABC-*", "/jira/rest/api/2/issue/ABC-123", True),
	("/jira/rest/api/2/issue/ABC-*", "/jira/rest/api/2/issue/ABC-1/comment", False),
	("/jira/*/issue", "/jira/x/issue", True),
	("/jira/*/issue", "/jira/x/y/issue", False),
	("/jira/**/issue", "/jira/issue", True),
	("/jira/**/issue", "/jira/a/b/issue", True),
	("/jira/**x", "/jirax", False),
	("/jira/**x", "/jira/abx", True),
	("/jira/rest/api/2/issue/ABC-123", "/jira/rest/api/2/issue/ABC-123", True),
	("/jira/rest/api/2/issue/ABC-123", "/jira/rest/api/2/issue/ABC-1234", False),
	("/jira/a.b", "/jira/aXb", False),
	("/jira/x", "/jira/x\n", False),
])
def test_pattern_matching(pattern, path, expected):
	assert bool(compile_pattern(pattern).fullmatch(path)) is expected


def test_pattern_must_start_with_slash():
	with pytest.raises(ValueError, match="must start with '/'"):
		compile_pattern("jira/**")


def test_specificity_counts_literals_and_wildcards():
	assert specificity("/jira/**") == (6, 1)
	assert specificity("/jira/rest/api/2/issue/ABC-*") == (27, 1)
	assert specificity("/jira/a*b*") == (8, 2)


def test_most_specific_wins_across_spec_examples():
	rs = RuleSet([
		rule(1, "/jira/**"),
		rule(2, "/jira/rest/**"),
		rule(3, "/jira/rest/api/2/**"),
		rule(4, "/jira/rest/api/2/issue/ABC-*"),
		rule(5, "/jira/rest/api/2/issue/ABC-123"),
	])
	assert rs.match("jira", "/jira/rest/api/2/issue/ABC-123").id == 5
	assert rs.match("jira", "/jira/rest/api/2/issue/ABC-9").id == 4
	assert rs.match("jira", "/jira/rest/api/2/search").id == 3
	assert rs.match("jira", "/jira/rest/other").id == 2
	assert rs.match("jira", "/jira/plugins/x").id == 1
	assert rs.match("jira", "/confluence/x") is None
	assert rs.match("wiki", "/jira/rest/api/2/issue/ABC-123") is None


def test_fewer_wildcards_breaks_literal_tie():
	rs = RuleSet([rule(1, "/jira/a*b*"), rule(2, "/jira/ab*")])
	assert rs.match("jira", "/jira/abc").id == 2


def test_longer_pattern_breaks_literal_and_wildcard_tie():
	rs = RuleSet([rule(1, "/jira/*"), rule(2, "/jira/**")])
	assert rs.match("jira", "/jira/x").id == 2


def test_newest_rule_breaks_full_tie():
	rs = RuleSet([rule(1, "/jira/a*"), rule(2, "/jira/*a")])
	assert rs.match("jira", "/jira/aa").id == 2


def test_disabled_rules_are_ignored():
	rs = RuleSet([rule(1, "/jira/**"), rule(2, "/jira/x", enabled=False)])
	assert rs.match("jira", "/jira/x").id == 1
	assert len(rs) == 1


def test_bypass_property():
	assert rule(1, "/a", ttl_seconds=0).bypass is True
	assert rule(1, "/a", ttl_seconds=None).bypass is False
	assert rule(1, "/a", ttl_seconds=60).bypass is False


def test_a_rule_matches_only_its_own_upstream():
	rs = RuleSet([rule(1, "/rest/**", upstream_id="jira"), rule(2, "/rest/api/**", upstream_id="wiki")])
	assert rs.match("jira", "/rest/api/2/issue/A-1").id == 1   # wiki's more specific rule never competes
	assert rs.match("wiki", "/rest/api/space").id == 2
	assert rs.match("wiki", "/rest/other") is None
	assert rs.match("conf", "/rest/api/x") is None
	assert [r.id for r in rs.match_all("jira", "/rest/api/x")] == [1]
	assert len(rs) == 2
