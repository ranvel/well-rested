# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-shelf LRU eviction under a byte budget, and the startup sweep of orphan partitions."""

import shelve

from proxy import db
from proxy.cache import ANONYMOUS, LAYOUT, CacheStore, build_key, make_entry

HEADERS = [("Content-Type", "application/json")]
BODY = b"x" * 100
ENTRY_BYTES = len(BODY) + sum(len(n) + len(v) for n, v in HEADERS) + 256   # 384
TWO = 2 * ENTRY_BYTES + 10   # a budget that fits two entries and not three


def entry(fetched_at: float, **overrides):
	fields = dict(
		status=200, headers=HEADERS, body=BODY, url="/x", identity="anonymous",
		rule_id=1, ttl=60, fetched_at=fetched_at,
	)
	fields.update(overrides)
	return make_entry(**fields)


async def keys(store: CacheStore, upstream_id: str = "jira") -> set[str]:
	return {k for k in ("A", "B", "C", "D") if await store.get(upstream_id, k) is not None}


# --- eviction ------------------------------------------------------------------

async def test_lru_eviction_on_write(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await store.put("jira", "A", entry(1000.0))
	await store.put("jira", "B", entry(1001.0))
	await store.put("jira", "C", entry(1002.0))
	assert await keys(store) == {"B", "C"}
	assert store._totals["jira"] == 2 * ENTRY_BYTES
	await store.put("jira", "D", entry(1003.0))
	assert await keys(store) == {"C", "D"}
	assert store._totals["jira"] == 2 * ENTRY_BYTES
	store.close()


async def test_record_hit_protects_an_entry(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await store.put("jira", "A", entry(1000.0))
	await store.put("jira", "B", entry(1001.0))
	store.record_hit("jira", "A", 1500.0)
	await store.put("jira", "C", entry(1002.0))
	assert await keys(store) == {"A", "C"}
	store.close()


async def test_never_ttl_entries_are_evictable(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await store.put("jira", "A", entry(1000.0, ttl=None))
	await store.put("jira", "B", entry(1001.0))
	await store.put("jira", "C", entry(1002.0))
	assert await keys(store) == {"B", "C"}
	store.close()


async def test_just_written_entry_survives_its_own_pass(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=ENTRY_BYTES // 2)
	await store.put("jira", "A", entry(1000.0))
	assert await keys(store) == {"A"}
	await store.put("jira", "B", entry(1001.0))
	assert await keys(store) == {"B"}
	assert store._totals["jira"] == ENTRY_BYTES
	store.close()


async def test_eviction_uses_stored_access_after_restart(tmp_path):
	first = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await first.put("jira", "A", entry(1000.0))
	await first.put("jira", "B", entry(1001.0))
	first.close()
	second = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await second.put("jira", "C", entry(1002.0))
	assert await keys(second) == {"B", "C"}
	second.close()


async def test_purge_keeps_totals_consistent(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await store.put("jira", "A", entry(1000.0, url="/a"))
	await store.put("jira", "B", entry(1001.0, url="/b"))
	assert await store.purge_by_path("jira", "/a", "anonymous") == 1
	assert store._totals["jira"] == ENTRY_BYTES
	await store.put("jira", "C", entry(1002.0))
	assert await keys(store) == {"B", "C"}
	await store.put("jira", "D", entry(1003.0))
	assert await keys(store) == {"C", "D"}
	assert store._totals["jira"] == 2 * ENTRY_BYTES
	store.close()


async def test_replacing_an_entry_counts_its_size_once(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=TWO)
	await store.put("jira", "A", entry(1000.0))
	await store.put("jira", "A", entry(1001.0, body=b"y" * 50))
	assert store._totals["jira"] == ENTRY_BYTES - 50
	store.close()


# --- the startup orphan sweep ----------------------------------------------------

async def test_sweep_removes_orphan_partitions(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	partitions = {"anon": ANONYMOUS, "live": "jira-uat#3", "old": "old#1", "shared": "", "plan1": "basic:alice"}
	for key, identity in partitions.items():
		await store.put("jira", key, entry(1000.0, identity=identity))
	assert await store.sweep_orphans({"jira": {"jira-uat#3"}}) == 3
	survivors = {key for key in partitions if await store.get("jira", key) is not None}
	assert survivors == {"anon", "live"}
	assert store._totals["jira"] == 2 * ENTRY_BYTES
	store.close()


async def test_sweep_discovers_shelves_on_disk(tmp_path):
	first = CacheStore(tmp_path / "_cache")
	await first.put("jira", "A", entry(1000.0, identity="old#1"))
	await first.put("wiki", "B", entry(1000.0, identity="old#1", url="/wiki/x"))
	first.close()
	second = CacheStore(tmp_path / "_cache")
	assert await second.sweep_orphans({}) == 2
	assert await second.get("jira", "A") is None
	assert await second.get("wiki", "B") is None
	second.close()


async def test_sweep_tolerates_non_dict_values(tmp_path):
	cache_dir = tmp_path / "_cache"
	cache_dir.mkdir()
	(cache_dir / "LAYOUT").write_text(LAYOUT)   # written by this layout, so kept
	with shelve.open(str(cache_dir / "jira.shelf")) as raw:
		raw["junk"] = "not an entry"
	store = CacheStore(cache_dir)
	await store.put("jira", "A", entry(1000.0))
	assert await store.sweep_orphans({}) == 1
	assert await store.get("jira", "junk") is None
	assert await store.get("jira", "A") is not None
	store.close()


def test_startup_sweep_runs(settings, make_client, add_identity):
	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	conn.close()
	identity = add_identity()
	live_key = build_key(identity.cache_identity, "/x", "")
	orphan_key = build_key("ghost#9", "/x", "")
	settings.cache_dir.mkdir()
	(settings.cache_dir / "LAYOUT").write_text(LAYOUT)
	with shelve.open(str(settings.cache_dir / "jira.shelf")) as raw:
		raw[live_key] = entry(0.0, identity=identity.cache_identity, ttl=None)
		raw[orphan_key] = entry(0.0, identity="ghost#9", ttl=None)
	client = make_client()
	conn = db.connect(settings.db_path)
	db.create_rule(conn, "jira", "/**", None)
	conn.close()
	cache = client.app.state.cache
	assert client.portal.call(cache.get, "jira", orphan_key) is None
	assert client.get("/x", headers={"X-WR-Identity": "jira-uat"}).headers["x-cache"] == "HIT"


async def test_sweep_keeps_a_partition_only_on_the_shelves_it_is_bound_to(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "J", entry(1000.0, identity="ran#1"))
	await store.put("wiki", "W", entry(1000.0, identity="ran#1", url="/wiki/x"))
	await store.put("wiki", "A", entry(1000.0, identity=ANONYMOUS, url="/wiki/y"))
	assert await store.sweep_orphans({"jira": {"ran#1"}}) == 1   # live, but no longer bound to /wiki
	assert await store.get("jira", "J") is not None
	assert await store.get("wiki", "W") is None
	assert await store.get("wiki", "A") is not None
	store.close()


def test_startup_sweep_drops_entries_of_an_unbound_identity(settings, make_client, add_identity):
	conn = db.connect(settings.db_path)
	db.put_upstream(conn, db.Upstream("jira", "https://jira.test"))
	db.put_upstream(conn, db.Upstream("wiki", "https://wiki.test"))
	conn.close()
	identity = add_identity(upstream_id="jira")   # was bound to /wiki too, once
	settings.cache_dir.mkdir()
	(settings.cache_dir / "LAYOUT").write_text(LAYOUT)
	jira_key = build_key(identity.cache_identity, "/x", "")
	wiki_key = build_key(identity.cache_identity, "/wiki/x", "")
	with shelve.open(str(settings.cache_dir / "jira.shelf")) as raw:
		raw[jira_key] = entry(0.0, identity=identity.cache_identity, ttl=None)
	with shelve.open(str(settings.cache_dir / "wiki.shelf")) as raw:
		raw[wiki_key] = entry(0.0, identity=identity.cache_identity, ttl=None, url="/wiki/x")
	client = make_client()
	cache = client.app.state.cache
	assert client.portal.call(cache.get, "jira", jira_key) is not None
	assert client.portal.call(cache.get, "wiki", wiki_key) is None
