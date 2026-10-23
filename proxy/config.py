# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Runtime settings, loaded from PROXY_* environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
	db_path: Path = Path("proxy.db")
	cache_dir: Path = Path("_cache")
	upstreams_seed: str = ""                     # PROXY_UPSTREAMS, applied on first run only
	max_response_bytes: int = 10 * 1024 * 1024   # larger responses pass through uncached
	host: str = "127.0.0.1"                      # 127.0.0.1 by default; 0.0.0.0 serves loopback and LAN clients
	port: int = 1337
	log_level: str = "INFO"
	log_tags: str = ""                           # comma-separated allowlist; empty = all
	allowed_hosts: str = "localhost,127.0.0.1,[::1]"   # PROXY_ALLOWED_HOSTS; Host values the proxy answers
	approval_wait_s: float = 25.0                # how long a request waits on an approval prompt
	approval_prompt_timeout_s: float = 120.0     # how long the dialog stays on screen
	approval_cooldown_s: float = 300.0           # how long a "No" answer silences re-prompting
	flush_seconds: float = 60.0                  # how often in-memory hit/access metadata is written back to the shelves
	cache_max_bytes: int = 500 * 1024 * 1024     # per-shelf LRU budget; "never expires" does not mean "never evicted"

	@classmethod
	def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
		env = os.environ if env is None else env
		d = cls()
		return cls(
			db_path=Path(env.get("PROXY_DB_PATH", d.db_path)),
			cache_dir=Path(env.get("PROXY_CACHE_DIR", d.cache_dir)),
			upstreams_seed=env.get("PROXY_UPSTREAMS", d.upstreams_seed),
			max_response_bytes=int(env.get("PROXY_MAX_RESPONSE_BYTES", d.max_response_bytes)),
			host=env.get("PROXY_HOST", d.host),
			port=int(env.get("PROXY_PORT", d.port)),
			log_level=env.get("PROXY_LOG_LEVEL", d.log_level),
			log_tags=env.get("PROXY_LOG_TAGS", d.log_tags),
			allowed_hosts=env.get("PROXY_ALLOWED_HOSTS", d.allowed_hosts),
			flush_seconds=float(env.get("PROXY_FLUSH_SECONDS", d.flush_seconds)),
			cache_max_bytes=int(env.get("PROXY_CACHE_MAX_BYTES", d.cache_max_bytes)),
		)


def parse_upstreams_seed(seed: str) -> list[tuple[str, str]]:
	"""Parse "jira=https://jira.example.com,confluence=https://..." into (upstream_id, base_url) pairs."""
	pairs = []
	for entry in seed.split(","):
		entry = entry.strip()
		if not entry:
			continue
		upstream_id, sep, base_url = entry.partition("=")
		if not sep or not upstream_id.strip() or not base_url.strip():
			raise ValueError(f"bad PROXY_UPSTREAMS entry {entry!r}; expected <upstream id>=https://host")
		pairs.append((upstream_id.strip(), base_url.strip()))
	return pairs
