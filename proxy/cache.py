# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Shelve-backed response cache: one shelf per upstream under the cache dir.

shelve/dbm is not concurrency-safe, so each shelf is guarded by a per-upstream
asyncio.Lock. The lock is held only around a single read or write; callers must
never hold it across an upstream fetch (the request flow is: get → fetch → put).
"""

import asyncio
import hashlib
import re
import shelve
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from proxy.db import UPSTREAM_ID_RE
from proxy.log import get_logger

log = get_logger(__name__, "CACHE")


def normalize_query(raw_query: str, query_mode: str) -> str:
	"""Canonical query string for the cache key, per the rule's query_mode.

	include: params sorted by name. The sort is stable, so repeated names keep their
	         original order (?id=2&id=1 stays distinct from ?id=1&id=2).
	ignore / reject: empty (reject only reaches here when there is no query).
	"""
	if query_mode != "include" or not raw_query:
		return ""
	pairs = parse_qsl(raw_query, keep_blank_values=True, encoding="latin-1")
	return urlencode(sorted(pairs, key=lambda kv: kv[0]), encoding="latin-1")   # latin-1 round-trips every byte


ANONYMOUS = "anonymous"   # partition for requests without X-WR-Identity


def build_key(identity: str, path: str, normalized_query: str) -> str:
	"""Full sha256 hex of identity|path?query. Identity is never truncated here."""
	return hashlib.sha256(f"{identity}|{path}?{normalized_query}".encode("utf-8")).hexdigest()


def make_entry(
	*,
	status: int,
	headers: list[tuple[str, str]],
	body: bytes,
	url: str,
	identity: str,
	rule_id: int,
	ttl: int | None,
	fetched_at: float | None = None,
	hits: int = 0,
) -> dict:
	"""A cache value. headers must already be cacheable (see headers.cacheable_response_headers).
	A refresh passes the old entry's hits so popularity survives a TTL lapse."""
	fetched = time.time() if fetched_at is None else fetched_at
	content_type = next((v for n, v in headers if n.lower() == "content-type"), "")
	return {
		"status": status,
		"headers": list(headers),
		"body": body,
		"content_type": content_type,
		"fetched_at": fetched,
		"ttl": ttl,                  # None = never expires
		"url": url,                  # human-readable, for the cache browser
		"identity": identity,        # partition: "<name>#<id>" or "anonymous"
		"rule_id": rule_id,
		"hits": hits,                # the last flushed count; live deltas live in CacheStore
		"last_accessed": fetched,
	}


_ENTRY_TTL = object()


def is_fresh(entry: dict, now: float | None = None, ttl=_ENTRY_TTL) -> bool:
	"""Fresh when younger than the ttl. A passed ttl (None = never) overrides the entry's frozen one."""
	if ttl is _ENTRY_TTL:
		ttl = entry["ttl"]
	if ttl is None:
		return True
	now = time.time() if now is None else now
	return now - entry["fetched_at"] < ttl


def age_seconds(entry: dict, now: float | None = None) -> int:
	now = time.time() if now is None else now
	return max(0, int(now - entry["fetched_at"]))


_ENTRY_OVERHEAD = 256   # pickle/dbm slack per entry; close enough for a byte budget


def _entry_bytes(entry) -> int:
	if not isinstance(entry, dict):
		return _ENTRY_OVERHEAD
	headers = entry.get("headers") or []
	return len(entry.get("body", b"")) + sum(len(n) + len(v) for n, v in headers) + _ENTRY_OVERHEAD


# The key layout, recorded in <cache_dir>/LAYOUT. No marker means the layout from before header
# routing, whose keys and urls began with "/<upstream>"; those entries can never be hit again.
LAYOUT = "2"
_LAYOUT_FILE = "LAYOUT"
# A shelf's files: <upstream_id>.shelf plus whatever suffix the dbm backend adds.
_SHELF_FILE_RE = re.compile(f"({UPSTREAM_ID_RE.pattern})" + r"\.shelf(?:\.(?:db|dat|dir|bak))?(?:-wal|-shm|-journal)?")


class CacheStore:
	def __init__(self, cache_dir: Path, max_bytes: int = 500 * 1024 * 1024):
		self._dir = Path(cache_dir)
		self._dir.mkdir(parents=True, exist_ok=True)
		self._check_layout()
		self._max_bytes = max_bytes
		self._shelves: dict[str, shelve.Shelf] = {}
		self._locks: dict[str, asyncio.Lock] = {}
		self._live: dict[tuple[str, str], list] = {}      # (upstream_id, key) → [unflushed hits, last_accessed]
		self._index: dict[str, dict[str, list]] = {}      # upstream_id → key → [last_accessed, bytes]
		self._totals: dict[str, int] = {}                 # upstream_id → Σ bytes

	def _check_layout(self) -> None:
		"""Delete every shelf file written under another key layout, then record this one.
		Runs before any shelf is opened; files that aren't shelves are left alone."""
		marker = self._dir / _LAYOUT_FILE
		try:
			if marker.read_text().strip() == LAYOUT:
				return
		except FileNotFoundError:
			pass
		doomed = [path for path in self._dir.iterdir() if path.is_file() and _SHELF_FILE_RE.fullmatch(path.name)]
		for path in doomed:
			path.unlink()
		marker.write_text(LAYOUT + "\n")
		if doomed:
			log.info("cache layout changed; shelves cleared; files=%d", len(doomed))

	def _lock(self, upstream_id: str) -> asyncio.Lock:
		"""Every shelf access starts here, so a name that isn't an upstream id ("../x") never becomes a path."""
		if not UPSTREAM_ID_RE.fullmatch(upstream_id):
			raise ValueError(f"not an upstream id: {upstream_id[:64]!r}")
		lock = self._locks.get(upstream_id)
		if lock is None:
			lock = self._locks[upstream_id] = asyncio.Lock()
		return lock

	def _shelf(self, upstream_id: str) -> shelve.Shelf:
		"""Open lazily, building the size/access index with one scan. Call only with the upstream's lock held."""
		shelf = self._shelves.get(upstream_id)
		if shelf is None:
			shelf = self._shelves[upstream_id] = shelve.open(str(self._dir / f"{upstream_id}.shelf"), writeback=False)
			index: dict[str, list] = {}
			total = 0
			for key, entry in shelf.items():
				size = _entry_bytes(entry)
				accessed = entry.get("last_accessed", 0.0) if isinstance(entry, dict) else 0.0
				index[key] = [accessed, size]
				total += size
			self._index[upstream_id] = index
			self._totals[upstream_id] = total
			log.debug("shelf opened; upstream=%s entries=%d bytes=%d", upstream_id, len(index), total)
		return shelf

	def _discover_upstream_ids(self) -> set[str]:
		"""Shelf upstream ids on disk (dbm may append suffixes like .db or -wal) plus any already open."""
		found = set(self._shelves)
		for path in self._dir.iterdir():
			match = _SHELF_FILE_RE.fullmatch(path.name)   # anything else was never written by this store
			if match:
				found.add(match.group(1))
		return found

	async def get(self, upstream_id: str, key: str) -> dict | None:
		async with self._lock(upstream_id):
			return self._shelf(upstream_id).get(key)

	async def put(self, upstream_id: str, key: str, entry: dict) -> None:
		async with self._lock(upstream_id):
			shelf = self._shelf(upstream_id)
			shelf[key] = entry
			index = self._index[upstream_id]
			if key in index:
				self._totals[upstream_id] -= index[key][1]
			size = _entry_bytes(entry)
			index[key] = [entry["last_accessed"], size]
			self._totals[upstream_id] += size
			self._evict(upstream_id, shelf, keep=key)
			shelf.sync()

	def _access_time(self, upstream_id: str, key: str) -> float:
		live = self._live.get((upstream_id, key))
		return live[1] if live is not None else self._index[upstream_id][key][0]

	def _evict(self, upstream_id: str, shelf: shelve.Shelf, keep: str) -> int:
		"""LRU-evict until back under budget; never the key just written. Lock held by the caller."""
		index = self._index[upstream_id]
		evicted = bytes_freed = 0
		while self._totals[upstream_id] > self._max_bytes:
			victim = min(
				(k for k in index if k != keep),
				key=lambda k: self._access_time(upstream_id, k),
				default=None,
			)
			if victim is None:
				break   # only the just-written entry is left; it stays
			del shelf[victim]
			bytes_freed += index[victim][1]
			self._totals[upstream_id] -= index.pop(victim)[1]
			self._live.pop((upstream_id, victim), None)
			evicted += 1
		if evicted:
			log.info("evicted; upstream=%s entries=%d bytes=%d", upstream_id, evicted, bytes_freed)
		return evicted

	def record_hit(self, upstream_id: str, key: str, now: float | None = None) -> None:
		"""A HIT was served. In memory only; the shelf sees it at the next flush."""
		now = time.time() if now is None else now
		live = self._live.get((upstream_id, key))
		if live is None:
			self._live[(upstream_id, key)] = [1, now]
		else:
			live[0] += 1
			live[1] = now
		index = self._index.get(upstream_id)
		if index is not None and key in index:
			index[key][0] = now

	def display_meta(self, upstream_id: str, key: str, entry: dict) -> tuple[int, float]:
		"""(hits, last_accessed) as the cache browser should show them: stored + live delta."""
		live = self._live.get((upstream_id, key))
		if live is None:
			return entry["hits"], entry["last_accessed"]
		return entry["hits"] + live[0], max(entry["last_accessed"], live[1])

	async def flush_meta(self) -> int:
		"""Merge unflushed hit/access metadata into the shelves. Returns entries updated."""
		pending, self._live = self._live, {}   # hits recorded mid-flush land in the fresh map
		by_upstream: dict[str, list[tuple[str, int, float]]] = {}
		for (upstream_id, key), (delta, last) in pending.items():
			by_upstream.setdefault(upstream_id, []).append((key, delta, last))
		updated = 0
		for upstream_id, rows in by_upstream.items():
			async with self._lock(upstream_id):
				shelf = self._shelf(upstream_id)
				changed = False
				for key, delta, last in rows:
					entry = shelf.get(key)
					if entry is None:
						continue   # purged or evicted since; the delta dies with it
					entry["hits"] += delta
					entry["last_accessed"] = max(entry["last_accessed"], last)   # a refresh is never regressed
					shelf[key] = entry
					changed = True
					updated += 1
				if changed:
					shelf.sync()
		if updated:
			log.debug("meta flushed; entries=%d", updated)
		return updated

	async def hits_by_rule(self) -> dict[int, int]:
		"""Total displayed hits per rule id, across every shelf. An admin-page scan, not a hot path."""
		totals: dict[int, int] = {}
		for upstream_id in sorted(self._discover_upstream_ids()):
			async with self._lock(upstream_id):
				shelf = self._shelf(upstream_id)
				for key, entry in shelf.items():
					if not isinstance(entry, dict):
						continue
					hits, _ = self.display_meta(upstream_id, key, entry)
					totals[entry["rule_id"]] = totals.get(entry["rule_id"], 0) + hits
		return totals

	def known_upstream_ids(self) -> set[str]:
		"""Every upstream id with a shelf, open or on disk (for the cache browser's upstream list)."""
		return self._discover_upstream_ids()

	async def browse(self, upstream_id: str) -> list[dict]:
		"""The cache browser's rows for one shelf: url, identity, age, ttl, size, hits, rule, key."""
		rows: list[dict] = []
		async with self._lock(upstream_id):
			shelf = self._shelf(upstream_id)
			index = self._index[upstream_id]
			for key, entry in shelf.items():
				if not isinstance(entry, dict):
					continue
				hits, last = self.display_meta(upstream_id, key, entry)
				rows.append({
					"key": key,
					"url": entry["url"],
					"identity": entry["identity"],
					"age_s": age_seconds(entry),
					"ttl": entry["ttl"],
					"fresh": is_fresh(entry),
					"bytes": index[key][1],
					"hits": hits,
					"last_accessed": last,
					"rule_id": entry["rule_id"],
				})
		rows.sort(key=lambda r: r["last_accessed"], reverse=True)
		return rows

	async def _purge_where(self, upstream_id: str, doom) -> int:
		"""Delete every entry for which doom(key, entry) holds; one scan under the lock."""
		async with self._lock(upstream_id):
			shelf = self._shelf(upstream_id)
			doomed = [key for key, entry in shelf.items() if doom(key, entry)]
			for key in doomed:
				del shelf[key]
			if doomed:
				shelf.sync()
		for key in doomed:
			self._forget(upstream_id, key)
		return len(doomed)

	async def purge_by_path(self, upstream_id: str, path: str, identity: str) -> int:
		"""Delete this partition's entries whose stored path equals path, every query variant.

		Called after a 2xx mutation. The scan runs under the shelf lock; shelves are
		size-capped, so O(entries) per mutation is acceptable.
		"""
		purged = await self._purge_where(
			upstream_id,
			lambda k, e: isinstance(e, dict)
			and e.get("identity") == identity and e.get("url", "").split("?", 1)[0] == path,
		)
		if purged:
			log.debug("purged by path; upstream=%s path=%s count=%d", upstream_id, path, purged)
		return purged

	async def purge_entry(self, upstream_id: str, key: str) -> bool:
		return await self._purge_where(upstream_id, lambda k, e: k == key) == 1

	async def purge_by_rule(self, rule_id: int) -> int:
		purged = 0
		for upstream_id in sorted(self._discover_upstream_ids()):
			purged += await self._purge_where(
				upstream_id, lambda k, e: isinstance(e, dict) and e.get("rule_id") == rule_id,
			)
		log.info("purged by rule; rule=%d count=%d", rule_id, purged)
		return purged

	async def purge_by_identity(self, identity: str) -> int:
		purged = 0
		for upstream_id in sorted(self._discover_upstream_ids()):
			purged += await self._purge_where(
				upstream_id, lambda k, e: isinstance(e, dict) and e.get("identity") == identity,
			)
		log.info("purged by identity; identity=%s count=%d", identity, purged)
		return purged

	async def purge_all(self, upstream_id: str) -> int:
		purged = await self._purge_where(upstream_id, lambda k, e: True)
		log.info("purged shelf; upstream=%s count=%d", upstream_id, purged)
		return purged

	def _forget(self, upstream_id: str, key: str) -> None:
		"""Drop in-memory metadata for a key that left the shelf."""
		self._live.pop((upstream_id, key), None)
		index = self._index.get(upstream_id)
		if index is not None and key in index:
			self._totals[upstream_id] -= index.pop(key)[1]

	async def sweep_orphans(self, live_partitions: dict[str, set[str]]) -> int:
		"""Startup sweep: delete every entry whose partition isn't live on its shelf.

		live_partitions maps an upstream id to the `<name>#<id>` partitions of the identities bound
		to it. Keeps anonymous and those; everything else goes — deleted or unbound identities,
		the retired shared partition, Plan 1's basic:/token: keys, and any value that isn't an
		entry dict at all.
		"""
		removed = 0
		for upstream_id in sorted(self._discover_upstream_ids()):
			keep = live_partitions.get(upstream_id, set()) | {ANONYMOUS}
			async with self._lock(upstream_id):
				shelf = self._shelf(upstream_id)
				doomed = [
					key for key, entry in shelf.items()
					if not isinstance(entry, dict) or entry.get("identity") not in keep
				]
				for key in doomed:
					del shelf[key]
				if doomed:
					shelf.sync()
			for key in doomed:
				self._forget(upstream_id, key)
			removed += len(doomed)
		if removed:
			log.info("orphan entries swept; count=%d", removed)
		return removed

	def close(self) -> None:
		for shelf in self._shelves.values():
			shelf.close()
		self._shelves.clear()
