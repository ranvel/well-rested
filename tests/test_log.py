# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import io

from proxy import log


def test_messages_carry_bracket_prefix():
	buf = io.StringIO()
	log.configure(level="DEBUG", stream=buf)
	log.get_logger("proxy.testlog", "TESTA").debug("hello; n=%d", 1)
	assert "DEBUG [TESTA] hello; n=1" in buf.getvalue()


def test_tag_allowlist_drops_other_tags():
	buf = io.StringIO()
	log.configure(level="DEBUG", tags="keep", stream=buf)
	log.get_logger("proxy.testlog", "KEEP").debug("visible")
	log.get_logger("proxy.testlog", "DROP").debug("hidden")
	out = buf.getvalue()
	assert "[KEEP] visible" in out
	assert "hidden" not in out


def test_level_floor_suppresses_debug():
	buf = io.StringIO()
	log.configure(level="INFO", stream=buf)
	logger = log.get_logger("proxy.testlog", "LVL")
	logger.debug("quiet")
	logger.info("loud")
	out = buf.getvalue()
	assert "quiet" not in out
	assert "[LVL] loud" in out


def test_configure_is_repeatable_without_duplicate_output():
	buf = io.StringIO()
	log.configure(stream=buf)
	log.configure(stream=buf)
	log.get_logger("proxy.testlog", "ONCE").debug("x")
	assert buf.getvalue().count("[ONCE] x") == 1
