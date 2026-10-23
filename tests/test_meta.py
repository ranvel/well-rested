# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Hit counting and access tracking live in memory and reach the shelf only through a flush."""

import shelve
import time

import httpx
from fastapi.testclient import TestClient

from proxy import db
from proxy.cache import ANONYMOUS, CacheStore, build_key, make_entry
from proxy.keychain import MemorySecretStore
from proxy.main import create_app
from tests.test_caching import shift_clock

KEY = build_key(ANONYMOUS, "/x", "")


def entry(**overrides):
	fields = dict(
		status=200, headers=[("Content-Type", "application/json")], body=b'{"a":1}',
		url="/x", identity="anonymous", rule_id=1, ttl=60, fetched_at=1000.0,
	)
	fields.update(overrides)
	return make_entry(**fields)


class Writes:
	"""Counts every write that reaches the underlying dbm, and every Shelf.sync()."""

	def __init__(self, shelf: shelve.Shelf):
		self.sets = 0
		self.syncs = 0
		inner, counter = shelf.dict, self

		class Counting:
			def __setitem__(self, key, value):
				counter.sets += 1
				inner[key] = value

			def __getattr__(self, name):
				return getattr(inner, name)

			def __getitem__(self, key):
				return inner[key]

			def __delitem__(self, key):
				del inner[key]

			def __contains__(self, key):
				return key in inner

			def __iter__(self):
				return iter(inner)

			def __len__(self):
				return len(inner)

		shelf.dict = Counting()
		real_sync = shelf.sync

		def sync():
			counter.syncs += 1
			real_sync()

		shelf.sync = sync


async def warmed(tmp_path) -> tuple[CacheStore, Writes]:
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "k", entry())
	return store, Writes(store._shelves["jira"])


# --- store level ---------------------------------------------------------------

async def test_record_hit_never_writes_the_shelf(tmp_path):
	store, writes = await warmed(tmp_path)
	for i in range(50):
		store.record_hit("jira", "k", 2000.0 + i)
	assert (writes.sets, writes.syncs) == (0, 0)
	assert (await store.get("jira", "k"))["hits"] == 0
	store.close()


async def test_flush_merges_hits_and_last_accessed(tmp_path):
	store, writes = await warmed(tmp_path)
	for _ in range(3):
		store.record_hit("jira", "k", 2000.0)
	assert await store.flush_meta() == 1
	assert (writes.sets, writes.syncs) == (1, 1)
	stored = await store.get("jira", "k")
	assert stored["hits"] == 3
	assert stored["last_accessed"] == 2000.0
	store.close()


async def test_flush_never_regresses_last_accessed(tmp_path):
	store, _ = await warmed(tmp_path)
	store.record_hit("jira", "k", 1500.0)
	await store.put("jira", "k", entry(fetched_at=2000.0, hits=0))   # a refresh, carrying the stored count
	assert await store.flush_meta() == 1
	stored = await store.get("jira", "k")
	assert stored["last_accessed"] == 2000.0
	assert stored["hits"] == 1
	store.close()


async def test_display_meta_merges_live_delta(tmp_path):
	store, _ = await warmed(tmp_path)
	stored = await store.get("jira", "k")
	assert store.display_meta("jira", "k", stored) == (0, 1000.0)
	store.record_hit("jira", "k", 1200.0)
	store.record_hit("jira", "k", 1300.0)
	assert store.display_meta("jira", "k", stored) == (2, 1300.0)
	await store.flush_meta()
	stored = await store.get("jira", "k")
	assert store.display_meta("jira", "k", stored) == (2, 1300.0)
	store.close()


async def test_vanished_entry_drops_its_delta(tmp_path):
	store, _ = await warmed(tmp_path)
	store.record_hit("jira", "k", 1200.0)
	assert await store.purge_by_path("jira", "/x", "anonymous") == 1
	assert ("jira", "k") not in store._live
	assert await store.flush_meta() == 0
	store.record_hit("jira", "k", 1300.0)   # a hit on a key the shelf no longer holds
	assert await store.flush_meta() == 0
	assert store._live == {}
	store.close()


async def test_second_flush_only_merges_new_hits(tmp_path):
	store, _ = await warmed(tmp_path)
	store.record_hit("jira", "k", 1100.0)
	assert await store.flush_meta() == 1
	store.record_hit("jira", "k", 1200.0)
	store.record_hit("jira", "k", 1300.0)
	assert await store.flush_meta() == 1
	assert (await store.get("jira", "k"))["hits"] == 3
	assert await store.flush_meta() == 0
	store.close()


async def test_flush_spans_prefixes(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "k", entry())
	await store.put("wiki", "k", entry(url="/wiki/x"))
	store.record_hit("jira", "k", 1100.0)
	store.record_hit("wiki", "k", 1100.0)
	assert await store.flush_meta() == 2
	store.close()


async def test_hits_by_rule_sums_stored_and_live_across_shelves(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "a", entry(rule_id=1, hits=5))
	await store.put("jira", "b", entry(rule_id=1, url="/y"))
	await store.put("wiki", "c", entry(rule_id=2, url="/wiki/x", hits=1))
	store.record_hit("jira", "b", 1100.0)
	store.record_hit("jira", "b", 1200.0)
	store.record_hit("wiki", "c", 1100.0)
	assert await store.hits_by_rule() == {1: 7, 2: 2}
	store.close()
	reopened = CacheStore(tmp_path / "_cache")   # discovered from disk, nothing opened yet
	assert await reopened.hits_by_rule() == {1: 5, 2: 1}
	reopened.close()


# --- through the app ------------------------------------------------------------

def test_refresh_keeps_popularity(client, admin_db, monkeypatch):
	db.create_rule(admin_db, "jira", "/**", 60)
	assert [client.get("/x").headers["x-cache"] for _ in range(3)] == ["MISS", "HIT", "HIT"]
	shift_clock(monkeypatch, 61)
	assert client.get("/x").headers["x-cache"] == "REFRESH"
	cache = client.app.state.cache
	assert client.portal.call(cache.flush_meta) == 1
	assert client.portal.call(cache.get, "jira", KEY)["hits"] == 2


def test_final_flush_on_shutdown(settings, fake_upstream, secrets, prompter):
	app = create_app(settings, transport=httpx.MockTransport(fake_upstream), secrets=secrets, prompter=prompter, proxy_secrets=MemorySecretStore())
	with TestClient(app, headers={"X-WR-Upstream": "jira"}) as c:
		conn = db.connect(settings.db_path)
		db.create_rule(conn, "jira", "/**", 3600)
		conn.close()
		c.get("/x")
		assert c.get("/x").headers["x-cache"] == "HIT"
	with shelve.open(str(settings.cache_dir / "jira.shelf")) as shelf:
		assert shelf[KEY]["hits"] == 1


def test_periodic_flush_runs(make_client, settings):
	c = make_client(flush_seconds=0.05)
	conn = db.connect(settings.db_path)
	db.create_rule(conn, "jira", "/**", 3600)
	conn.close()
	c.get("/x")
	assert c.get("/x").headers["x-cache"] == "HIT"
	time.sleep(0.3)
	with shelve.open(str(settings.cache_dir / "jira.shelf")) as shelf:   # a second handle, while the app runs
		assert shelf[KEY]["hits"] == 1
