# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from pathlib import Path

import pytest

from proxy.config import Settings, parse_upstreams_seed


def test_defaults_bind_to_loopback():
	s = Settings.from_env({})
	assert s.host == "127.0.0.1"
	assert s.port == 1337
	assert s.db_path == Path("proxy.db")
	assert s.cache_dir == Path("_cache")
	assert s.max_response_bytes == 10 * 1024 * 1024
	assert s.upstreams_seed == ""
	assert s.log_level == "INFO"
	assert s.flush_seconds == 60.0
	assert s.cache_max_bytes == 500 * 1024 * 1024


def test_env_overrides():
	s = Settings.from_env({
		"PROXY_DB_PATH": "/data/p.db",
		"PROXY_CACHE_DIR": "/data/_cache",
		"PROXY_UPSTREAMS": "jira=https://jira.test",
		"PROXY_MAX_RESPONSE_BYTES": "1024",
		"PROXY_HOST": "10.0.0.5",
		"PROXY_PORT": "9000",
		"PROXY_LOG_LEVEL": "INFO",
		"PROXY_LOG_TAGS": "CACHE,PROXY",
		"PROXY_FLUSH_SECONDS": "15",
		"PROXY_CACHE_MAX_BYTES": "1000000",
	})
	assert s.flush_seconds == 15.0
	assert s.cache_max_bytes == 1000000
	assert s.db_path == Path("/data/p.db")
	assert s.cache_dir == Path("/data/_cache")
	assert s.upstreams_seed == "jira=https://jira.test"
	assert s.max_response_bytes == 1024
	assert s.host == "10.0.0.5"
	assert s.port == 9000
	assert s.log_level == "INFO"
	assert s.log_tags == "CACHE,PROXY"


def test_parse_seed_trims_whitespace():
	seed = " jira=https://jira.example.com , confluence=https://wiki.example.com/ "
	assert parse_upstreams_seed(seed) == [
		("jira", "https://jira.example.com"),
		("confluence", "https://wiki.example.com/"),
	]


def test_parse_seed_empty_and_trailing_comma():
	assert parse_upstreams_seed("") == []
	assert parse_upstreams_seed("jira=https://j.test,") == [("jira", "https://j.test")]


@pytest.mark.parametrize("seed", ["jira", "=https://j.test", "jira="])
def test_parse_seed_rejects_malformed(seed):
	with pytest.raises(ValueError, match="bad PROXY_UPSTREAMS entry"):
		parse_upstreams_seed(seed)
