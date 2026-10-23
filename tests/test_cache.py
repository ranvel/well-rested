# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import hashlib

import pytest

from proxy.cache import (
	LAYOUT,
	CacheStore,
	age_seconds,
	build_key,
	is_fresh,
	make_entry,
	normalize_query,
)


def entry(**overrides):
	fields = dict(
		status=200, headers=[("Content-Type", "application/json")], body=b'{"a":1}',
		url="/x", identity="basic:jdoe", rule_id=1, ttl=60, fetched_at=1000.0,
	)
	fields.update(overrides)
	return make_entry(**fields)


def test_include_mode_sorts_params():
	assert normalize_query("b=2&a=1", "include") == "a=1&b=2"
	assert normalize_query("a=1&b=2", "include") == "a=1&b=2"


def test_include_mode_preserves_repeated_param_order():
	assert normalize_query("id=2&id=1", "include") == "id=2&id=1"
	assert normalize_query("id=2&id=1", "include") != normalize_query("id=1&id=2", "include")


def test_include_mode_keeps_blank_values():
	assert normalize_query("a=&b=1", "include") == "a=&b=1"


def test_normalize_query_is_byte_preserving():
	assert normalize_query("q=%E9", "include") != normalize_query("q=%E8", "include")
	assert normalize_query("q=%E9", "include") == "q=%E9"


def test_ignore_and_reject_drop_query():
	assert normalize_query("a=1", "ignore") == ""
	assert normalize_query("", "reject") == ""
	assert normalize_query("", "include") == ""


def test_key_is_full_sha256_of_identity_path_query():
	key = build_key("basic:jdoe", "/x", "a=1")
	assert key == hashlib.sha256(b"basic:jdoe|/x?a=1").hexdigest()
	assert len(key) == 64


def test_key_partitions_by_identity_and_query():
	base = build_key("basic:jdoe", "/x", "")
	assert base != build_key("basic:asmith", "/x", "")
	assert base != build_key("", "/x", "")
	assert base != build_key("basic:jdoe", "/x", "a=1")


def test_make_entry_fields():
	e = entry()
	assert e["content_type"] == "application/json"
	assert e["hits"] == 0
	assert e["last_accessed"] == e["fetched_at"] == 1000.0
	assert e["identity"] == "basic:jdoe"
	assert e["rule_id"] == 1


def test_make_entry_carries_hits_forward():
	assert entry(hits=7)["hits"] == 7   # a refresh keeps the old entry's popularity


def test_freshness_and_age():
	e = entry(ttl=60, fetched_at=1000.0)
	assert is_fresh(e, now=1059.9)
	assert not is_fresh(e, now=1060.0)
	assert age_seconds(e, now=1042.7) == 42
	assert age_seconds(e, now=999.0) == 0


def test_never_ttl_is_always_fresh():
	assert is_fresh(entry(ttl=None, fetched_at=0.0), now=10 ** 10)


async def test_put_then_get_round_trips(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "k1", entry())
	assert await store.get("jira", "k1") == entry()
	assert await store.get("jira", "missing") is None
	store.close()


async def test_entries_persist_across_reopen(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "k1", entry())
	store.close()
	reopened = CacheStore(tmp_path / "_cache")
	assert await reopened.get("jira", "k1") == entry()
	reopened.close()


async def test_one_shelf_per_prefix(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "k1", entry())
	await store.put("wiki", "k2", entry(url="/wiki/x"))
	assert await store.get("wiki", "k1") is None
	names = [p.name for p in (tmp_path / "_cache").iterdir()]
	assert any(n.startswith("jira.shelf") for n in names)
	assert any(n.startswith("wiki.shelf") for n in names)
	store.close()


async def test_purge_by_path_removes_every_query_variant(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "plain", entry(url="/rest/a"))
	await store.put("jira", "include", entry(url="/rest/a?x=1"))
	await store.put("jira", "ignore", entry(url="/rest/a?y=2"))   # an ignore-mode rule still stores the url
	assert await store.purge_by_path("jira", "/rest/a", "basic:jdoe") == 3
	for key in ("plain", "include", "ignore"):
		assert await store.get("jira", key) is None
	store.close()


async def test_purge_by_path_spares_other_partitions_and_paths(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "mine", entry(url="/rest/a"))
	await store.put("jira", "theirs", entry(url="/rest/a", identity="basic:asmith"))
	await store.put("jira", "other-path", entry(url="/rest/ab"))
	assert await store.purge_by_path("jira", "/rest/a", "basic:jdoe") == 1
	assert await store.get("jira", "mine") is None
	assert await store.get("jira", "theirs") is not None
	assert await store.get("jira", "other-path") is not None
	store.close()


async def test_purge_by_path_empty_shelf_is_zero(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	assert await store.purge_by_path("never", "/never/x", "anonymous") == 0
	store.close()


async def test_targeted_purges_keep_totals_consistent(tmp_path):
	store = CacheStore(tmp_path / "_cache", max_bytes=10 ** 9)
	await store.put("jira", "a", entry(url="/a", rule_id=1))
	await store.put("jira", "b", entry(url="/b", rule_id=2, identity="other"))
	await store.put("wiki", "c", entry(url="/wiki/c", rule_id=2))
	full, size_a = store._totals["jira"], store._index["jira"]["a"][1]
	assert await store.purge_entry("jira", "a") is True
	assert await store.purge_entry("jira", "a") is False
	assert store._totals["jira"] == full - size_a and "a" not in store._index["jira"]
	assert await store.purge_by_rule(2) == 2             # spans shelves
	assert store._totals["jira"] == 0 and store._totals["wiki"] == 0
	await store.put("jira", "d", entry(url="/d"))
	await store.put("jira", "e", entry(url="/e", identity="other"))
	assert await store.purge_by_identity("basic:jdoe") == 1
	assert await store.purge_all("jira") == 1
	assert store._totals["jira"] == 0 and store._index["jira"] == {}
	assert await store.purge_all("jira") == 0
	store.close()


async def test_browse_rows(tmp_path):
	store = CacheStore(tmp_path / "_cache")
	await store.put("jira", "a", entry(url="/a", fetched_at=1000.0, hits=4))
	await store.put("jira", "b", entry(url="/b?x=1", fetched_at=2000.0, identity="other"))
	store.record_hit("jira", "a", 3000.0)
	rows = await store.browse("jira")
	assert [r["key"] for r in rows] == ["a", "b"]   # most recently accessed first
	a = rows[0]
	assert (a["url"], a["identity"], a["hits"], a["last_accessed"], a["ttl"], a["rule_id"]) == ("/a", "basic:jdoe", 5, 3000.0, 60, 1)
	assert a["bytes"] == store._index["jira"]["a"][1]
	assert a["fresh"] is False and a["age_s"] > 0
	assert await store.browse("never") == []
	store.close()


def test_ttl_argument_overrides_entry_ttl():
	assert is_fresh(entry(ttl=None, fetched_at=1000.0), now=1100.0, ttl=60) is False
	assert is_fresh(entry(ttl=60, fetched_at=1000.0), now=1100.0, ttl=None) is True


def test_a_new_cache_dir_is_marked_with_the_layout(tmp_path):
	CacheStore(tmp_path / "_cache")
	assert (tmp_path / "_cache" / "LAYOUT").read_text().strip() == LAYOUT


async def test_shelves_from_before_header_routing_are_cleared(tmp_path):
	cache_dir = tmp_path / "_cache"
	old = CacheStore(cache_dir)
	await old.put("jira", "k", entry(url="/jira/x"))
	old.close()
	(cache_dir / "LAYOUT").unlink()                      # as left by a proxy that predates the marker
	(cache_dir / "notes.txt").write_text("not a shelf")
	(cache_dir / "my.shelfish-notes").write_text("not a shelf either")
	store = CacheStore(cache_dir)
	assert store.known_upstream_ids() == set()
	assert await store.get("jira", "k") is None
	assert (cache_dir / "notes.txt").exists() and (cache_dir / "my.shelfish-notes").exists()
	assert (cache_dir / "LAYOUT").read_text().strip() == LAYOUT


async def test_marked_shelves_are_kept(tmp_path):
	first = CacheStore(tmp_path / "_cache")
	await first.put("jira", "k", entry(url="/x"))
	first.close()
	assert (await CacheStore(tmp_path / "_cache").get("jira", "k"))["url"] == "/x"


@pytest.mark.parametrize("upstream_id", ["../x", "a/b", "", ".hidden", "_admin"])
async def test_a_shelf_name_must_be_an_upstream_id(tmp_path, upstream_id):
	store = CacheStore(tmp_path / "_cache")
	with pytest.raises(ValueError):
		await store.get(upstream_id, "k")
	with pytest.raises(ValueError):
		await store.purge_all(upstream_id)
	assert not (tmp_path / "x.shelf").exists()
