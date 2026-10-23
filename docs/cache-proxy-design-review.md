# Cache Proxy — Design Review

**Reviewed:** `docs/cache-proxy-design.md`
**Date:** 2026-09-02
**Reviewer:** Claude (Opus 4.8)
**Status of codebase:** greenfield — no implementation exists; this review is against the specification only.

---

## Summary

The design is strong and coherent: it addresses the parts of a caching proxy that are usually skipped, and its hardest feature — per-user cache partitioning — is handled with real care. Review surfaced **one security issue**, **three implementation-correctness bugs**, and **four design decisions** that needed an owner. All four decisions have been made (see below) and folded into an amended spec. Nothing here blocks implementation; the security item is resolved by an explicit, documented trust model.

---

## Strengths

- **Per-user partitioning done thoughtfully.** Identity derived from the decoded Basic-auth username where possible, `sha256` fallback for opaque tokens, raw credentials never persisted, and stale-if-error constrained to the *same* identity so an outage can't leak one user's data to another. This is the part most caching proxies get wrong.
- **Explicit, documented tiebreak order** for most-specific-wins resolution, backed by a "Test a URL" page that explains *which* rule won and *why*. That transparency is what makes rule-based caching operable in production.
- **One shelf per upstream prefix** — clean blast-radius containment for purges and bounded file sizes.
- **Resilience framed correctly** — stale-if-error as the headline win, single-flight stampede control, GET-only retries on idempotent methods.

---

## Findings by severity

### 🔴 Security — auth bypass on cache HIT

Identity is derived from the Basic-auth **username only** (deliberately, to survive password rotation), and the proxy never validates credentials itself. On a cache HIT it serves **without any upstream call**. Chained:

1. `jdoe:realpass` misses → upstream validates → `200` cached under identity `jdoe`.
2. Attacker sends `Authorization: Basic base64(jdoe:anything)` (even an empty password) → identity still derives to `jdoe`.
3. HIT → attacker receives `jdoe`'s cached data. **No password was ever required.**

Anyone who knows a *username* can read that user's cached responses; the upstream's authentication is bypassed precisely on the hot path. (This is specific to the Basic-auth branch. The hash branch — `sha256(full token)` — is safe, since reproducing the hash requires the actual token.)

**Decision: Accept — trusted network.** The proxy is documented as trusting the identity header and requiring an authenticating layer (SSO reverse proxy / trusted network) in front of it; it must not be internet-exposed. Under that assumption the username-only identity is intentional and the bypass is out of scope. The amended spec adds a **Trust model** subsection stating this constraint plainly.

### 🟠 Correctness bugs (implementation-level)

1. **Header hygiene will corrupt bodies.** The spec strips hop-by-hop + `Set-Cookie` but is silent on `Content-Encoding` / `Content-Length` / `Transfer-Encoding`. httpx auto-decompresses, so caching a decoded body while replaying `Content-Encoding: gzip` makes clients gunzip plaintext and fail. **Fix:** store the body as httpx delivers it, drop `Content-Encoding`, recompute `Content-Length` on serve, strip `Transfer-Encoding`.

2. **Shelf lock must not wrap the upstream fetch.** All shelf access is serialized behind a per-prefix `asyncio.Lock`. If that lock is held during the network fetch, every request to that upstream — including plain cache HITs — stalls behind one slow origin call. **Fix:** hold the lock only around read and write; release it during the fetch. Stated explicitly in the amended spec.

3. **Cache key must use the full identity digest.** The `sha256` fallback is "displayed as the first 12 hex chars." If the *key* ever uses that truncation, cross-user collisions become feasible (2^48 space). **Fix:** key on the full digest; truncation is display-only. Clarified in the *Key* spec.

### 🟡 Design decisions (resolved)

| # | Issue | Decision | Rationale |
|---|-------|----------|-----------|
| 2 | Mutating PUT/POST passes straight through, so a cached GET for the same path stays stale until TTL even after this proxy has seen the write. | **Invalidate on mutation** | Purge exact-path GET entries (same identity + any `shared` entry) after a 2xx mutation. Tighter freshness; documented limitation that collection-level writes don't invalidate list/search GETs. |
| 3 | Per-entry `hits` counter turns every HIT into a locked shelf write on the hot read path. | **Approximate / in-memory** | Keep hits + last-accessed in process, flush periodically. Keeps writes off the read path; counts may lag/reset on crash. |
| 4 | 10 MB per-response cap but no store-level bound; "Never" TTL entries never expire. `_cache/` grows unbounded. | **Size cap + LRU eviction** | Per-shelf byte budget with LRU eviction, reusing the in-memory last-accessed from #3. "Never expires" now means "not TTL-expired," **not** "never evicted." |

Decisions #3 and #4 deliberately share one in-memory metadata structure (`{hits, last_accessed}` per key), so the hot path stays write-free while eviction retains recency data.

### 🔵 Assumptions now recorded in the spec

- Content negotiation (`Accept` / `Accept-Language`) is **not** part of the cache key — a JSON-only-REST assumption; safe for Jira/Confluence, stated so it isn't a silent trap.
- Upstream cache directives (`Cache-Control` / `ETag` / `Last-Modified`) are intentionally ignored in favor of rule-driven TTLs.
- Only `200` responses are cached (conservative; forgoes cacheable `301/308/404`).
- The admin UI should **warn** when `shared=1` is toggled on an auth'd upstream — silent cross-user serving is otherwise an easy operator error.

---

## Minor notes

- Range requests: a cached full `200` served to a `Range:` request can break strict clients — an edge case worth noting, not fixing for v1.
- Env-seeding "on first run" (assumption 4 in the design) needs a defined trigger (empty DB?) and idempotency.
- Single-worker is load-bearing, not incidental (shelve + asyncio locks). Throughput is capped at one core; acceptable for the stated internal use, but it's a hard architectural boundary.

---

## Outcome

All findings are either fixed in the amended spec, resolved by an explicit design decision, or recorded as a stated assumption. `docs/cache-proxy-design.md` has been updated so a fresh implementer can build `proxy/` from it without needing this review thread for context.
