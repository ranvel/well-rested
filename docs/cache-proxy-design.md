# Resilient REST Pass-Through Cache Proxy — Design

## Overview

A GET-only pass-through HTTP proxy with rule-based response caching and a web UI for managing those rules. Non-GET requests are proxied transparently and never cached. Cache *rules* live in SQLite; cached *responses* live in Python `shelve` databases under `_cache/`.

Example: an incoming request to `/jira/rest/api/2/issue/ABC-123` can be matched (most-specific-wins) by any of *(amended 2026-10-14: with header routing the request is `/rest/api/2/issue/ABC-123` with `X-WR-Upstream: jira`, and the patterns are the jira upstream's `/**`, `/rest/**` and so on; see the amendment at the end)*:

- `/jira/**` — everything under jira
- `/jira/rest/**` — all REST calls
- `/jira/rest/api/2/**` — everything on API v2
- `/jira/rest/api/2/issue/ABC-123` — this one issue
- `/jira/rest/api/2/issue/ABC-*` — all issues in project ABC

## Stack

- **Python 3.11+**, no exotic deps
- **FastAPI + uvicorn** for both the proxy and the admin UI (single process, two routers)
- **httpx.AsyncClient** for upstream calls (connection pooling, per-request timeouts)
- **SQLite** (stdlib `sqlite3`, WAL mode) for rules + upstream route config
- **shelve** in `_cache/` for cached response payloads
- **Jinja2 templates + HTMX** for the admin UI — no JS build step; `htmx.min.js` is **vendored** into `static/` (pinned, no CDN), so the UI works offline
- **macOS Keychain** (via `keyring`) for upstream API tokens — the only place a token is ever stored
- **`osascript`** for the client-approval prompt (the proxy runs in your GUI login session)

## Repository layout

```
proxy/
	__init__.py
	main.py          # FastAPI app factory, mounts admin + proxy routers
	config.py        # env/config loading (bind addr, db path, cache dir)
	db.py            # sqlite connection, schema migration, rule CRUD
	rules.py         # pattern compilation, matching, most-specific resolution
	cache.py         # shelve-backed cache store (keying, get/set/purge, locks)
	upstream.py      # httpx client, retries, stale-if-error logic
	identities.py    # Keychain loading, credential injection, token health
	approvals.py     # client approvals, single-flight prompts, osascript
	cli.py           # identity add/rotate/list/delete (until the admin UI exists)
	proxy_router.py  # the catch-all GET/any-method pass-through
	admin_router.py  # web UI + JSON API under /_admin
	templates/       # Jinja2: rule list, rule form, cache browser, test-a-url
	static/
_cache/            # shelve files (gitignored)
proxy.db           # sqlite (gitignored)
```

## Routing to upstreams

*(Superseded 2026-10-14: the `X-WR-Upstream` header selects the upstream, the path is forwarded untouched, and `prefix` is now `upstream_id`; see "Amendment: header routing" at the end.)*

~~First path segment selects the upstream.~~ Stored in SQLite so it's editable from the UI:

```sql
CREATE TABLE upstreams (
	prefix      TEXT PRIMARY KEY,   -- e.g. 'jira'
	base_url    TEXT NOT NULL,      -- e.g. 'https://jira.internal.example.com'
	verify_tls  INTEGER NOT NULL DEFAULT 1,
	timeout_s   REAL NOT NULL DEFAULT 30.0
);
```

~~`/jira/rest/api/2/issue/ABC-123` → `https://jira.internal.example.com/rest/api/2/issue/ABC-123`. Unknown prefix → 502 with a helpful body.~~ Now: `/rest/api/2/issue/ABC-123` with `X-WR-Upstream: jira` → `https://jira.internal.example.com/rest/api/2/issue/ABC-123`; an unknown upstream → `400` listing the configured ones.

### Identities

*(Amended 2026-10-10: replaces the earlier trust model and header-derived identities.)*

Clients never send upstream credentials. They name an identity instead:

```
X-WR-Identity: jira-uat
```

The proxy looks the identity up, injects the upstream credential header itself (`Authorization`, or the header a custom identity names; amendments 43 and 56), and never forwards `X-WR-Identity` upstream. The header value is exactly the identity name shown in the admin UI.

```sql
CREATE TABLE identities (
	id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: part of the cache partition
	name            TEXT NOT NULL UNIQUE COLLATE NOCASE, -- the exact X-WR-Identity value, e.g. 'jira-uat'
	prefix          TEXT NOT NULL REFERENCES upstreams(prefix),
	scheme          TEXT NOT NULL CHECK (scheme IN ('basic', 'bearer')),  -- 'custom' too since v6 (amendment 43)
	username        TEXT,               -- basic only (e.g. the Atlassian account email); not a secret
	-- header TEXT since v9: custom only, the header the value is sent under (amendment 56)
	issued_at       TEXT NOT NULL,      -- when the current token was minted: the full end of the health bar
	expires_at      TEXT NOT NULL,      -- when it dies: the empty end
	rotate_url      TEXT,               -- where to mint a replacement
	last_status     TEXT,               -- 'ok' | 'rejected' (upstream answered 401) | NULL
	last_status_at  TEXT,
	created_at      TEXT NOT NULL,
	updated_at      TEXT NOT NULL
);
```

- ~~**An identity belongs to exactly one upstream.**~~ *(Superseded 2026-10-12: an identity is bound to any number of upstreams through `identity_upstreams`, and `identities.prefix` is gone; see the amendment at the end.)* A request whose `X-WR-Identity` names an identity that isn't bound to the request's upstream gets a ~~`400`~~ `403` *(amended 2026-10-14)*, so a token is never sent to a host it wasn't bound to.
- **The token lives only in the macOS Keychain**, as a generic password with service `well-rested` and account = identity name. SQLite, shelves, logs and error bodies never see it.
- **Loading step:** at startup, after the database opens, every identity's token is read from the Keychain into an in-memory map. Saving a token (admin UI or CLI) writes the Keychain first, then refreshes the map. A missing Keychain item doesn't stop startup, and neither does a Keychain read that fails (locked, or permission denied). Either way the identity has no token, and requests using it get a `503` naming the identity and what's missing. A failed read is logged by error type and tried again at the next loading step (an identity change or a restart). The Keychain ACL trusts the `python3` binary that created the item, so a Python upgrade triggers a one-time Keychain permission prompt.
- **`expires_at` is mandatory.** Well RESTed is built around short-lived API tokens, so an identity without a declared expiry can't be saved.
- **Injection:** `basic` → `Authorization: Basic base64(username:token)`; `bearer` → `Authorization: Bearer <token>`; `custom` → `<header>: <token>` verbatim *(amendments 43 and 56)*. It applies to every method.
- **Client headers:** a request carrying `Authorization` gets a `400` telling the client to use `X-WR-Identity`. `Cookie` is stripped, and so is any client header named like the one the identity injects *(amendment 56)*. An unknown identity gets a `400`, never a fallback to `anonymous`. A request with no `X-WR-Identity` is `anonymous` and gets no credentials injected.
- **Rotation** replaces the Keychain item and `issued_at`/`expires_at`, and clears `last_status`. The identity name doesn't change, so cached entries and approvals survive rotation. This keeps the rotation immunity that username-derived identities used to provide, without their impersonation hole.
- **Deleting an identity** deletes its Keychain item and its approvals. Its cache entries become unreachable at once, because the partition includes the never-reused id, and LRU eviction removes them later. A re-created identity with the same name therefore starts with an empty cache and fresh approvals.
- **Before Plan 3** (no admin UI yet), identities are managed with `python3 -m proxy identity add|rotate|list|delete`. The token is read with `getpass`, never from argv or the environment. A name typed in any letter case finds the identity, and the command then uses the stored spelling everywhere, including the Keychain account. `identity delete` removes the Keychain item before the database row, and stops with nothing deleted if the Keychain refuses, so a token is never left behind without its identity.

### Token health

Each identity shows a battery-style bar: `remaining = (expires_at − now) / (expires_at − issued_at)`. It is green above 50%, amber from 20–50%, red below 20%, and empty at expiry. A **Rotate** link to `rotate_url` sits beside it.

- An upstream `401` on a request using an identity sets `last_status = 'rejected'`, and the bar shows red/rejected regardless of the date. The next `2xx` sets it back to `ok`. Status is written only when it changes, never per request.
- The proxy keeps forwarding after `expires_at`, because the date is human-entered and the upstream is the authority. But once health is red, past expiry, or rejected, responses for that identity carry `Warning: 299 - "token for 'jira-uat' expires 2026-10-14; rotate at <rotate_url>"` so scripts can surface it.

### Network trust and client approval

Well RESTed is a single-user Jig. It doesn't authenticate clients; it decides which machines may use which identities.

- **Bind:** `PROXY_HOST` defaults to `127.0.0.1`. To serve LAN clients as well, bind `0.0.0.0`. Binding a single LAN address would lock out local scripts.
- **Host allowlist:** `PROXY_ALLOWED_HOSTS` defaults to `localhost,127.0.0.1,[::1]`. When binding to the LAN, add the Mac's LAN name and address. Any other `Host` gets a `400`. This defeats DNS rebinding, including from browsers on machines that are already approved.
- **Client address** is the socket's peer address. `X-Forwarded-For` and similar headers are ignored.
- **Loopback clients** are approved implicitly for every identity.
- **The admin UI answers loopback clients only.** A LAN client can never reach `/_admin`, so it can never approve itself.
- **Non-loopback clients** using an identity need an approval for the pair `(client address, identity)`. Anonymous requests inject no credentials, so they need none.

When an unapproved pair arrives, the proxy asks, Little Snitch style:

> 192.0.2.111 is trying to connect to https://jira-uat.example.com using the "jira-uat" identity. Allow?

The choices are **Always**, **One hour**, **Session** (until Well RESTed restarts) and **No**. The prompt is an `osascript` `choose from list`, where Cancel counts as No. Pending prompts are also listed on the admin UI's approvals page and can be answered there. When both are open, **the first answer wins**: a decision recorded from the approvals page settles the pair and closes the on-screen dialog (its `osascript` process group is killed); a decision made in the dialog settles the pair and the page's pending row disappears on its next poll. Either way the decision is recorded exactly once *(implemented 2026-10-12; earlier wording said the dialog's later answer would be ignored)*.

- **Single-flight:** every request for the same pair waits on one prompt.
- **Waiting:** a request holds for up to 25 s. If the prompt is still open after that, it gets a `503` with `Retry-After: 5`. The prompt stays up, and the answer applies to the client's next retry. A prompt left unanswered for 2 minutes closes with no decision recorded.
- **No:** `403`, and the pair isn't prompted again for 5 minutes (an in-memory cooldown).
- **Storage:** Always approvals live in SQLite. One hour, Session and the No cooldowns live in memory and vanish on restart.

```sql
CREATE TABLE approvals (
	client_addr  TEXT NOT NULL,
	identity_id  INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
	granted_at   TEXT NOT NULL,
	PRIMARY KEY (client_addr, identity_id)
);
```

**"Always" means "don't ask me again", not "access forever".** An Always approval is still bounded by everything else: the token's own expiry and health, revocation from the approvals page, deletion of the identity, and the Host allowlist. Keeping tokens short-lived is what makes Always safe.

**Known limitation:** trust is keyed on IP address, not on the machine. An Always approval for 192.0.2.111 trusts whichever device holds that address next. Give regular clients DHCP reservations; short-lived tokens bound the damage either way.

### Per-identity caching

Every cache entry is partitioned by identity. The partition string is `<name>#<id>` for an identity, or `anonymous`. Because the id is never reused, rotation keeps an identity's cache, while a deleted and re-created name starts empty. The shelf never holds anything secret. Anonymous entries are never served to identities, and vice versa, and no identity is ever served another identity's entry. There is no way to share an entry across partitions *(amended 2026-10-11: the per-rule `shared` flag is removed; see the amendment at the end)*. A response that is identical for everyone is still fetched and cached once per identity.

Stale-if-error only ever serves a stale entry belonging to the **same identity** — an upstream outage must never leak one identity's data to another.

## Cache rules

*(Amended 2026-10-14: every rule belongs to one upstream, `upstream_id NOT NULL REFERENCES upstreams ON DELETE CASCADE`, and patterns are unique per upstream; schema v8. See the amendment at the end.)*

```sql
CREATE TABLE rules (
	id             INTEGER PRIMARY KEY,
	pattern        TEXT NOT NULL UNIQUE,  -- path pattern with wildcards
	ttl_seconds    INTEGER,               -- NULL = never expires; 0 = bypass (never cache)
	query_mode     TEXT NOT NULL DEFAULT 'include'
	               CHECK (query_mode IN ('include', 'ignore', 'reject')),
	enabled        INTEGER NOT NULL DEFAULT 1,
	note           TEXT,
	created_at     TEXT NOT NULL,
	updated_at     TEXT NOT NULL
);
```

### TTL presets (UI)

`1m, 2m, 10m, 1h, 9h, 1d` → stored as seconds. Plus:

- **Never** → `ttl_seconds = NULL` → cached entry never *TTL-expires*. Note: "never expires" ≠ "never evicted" — a Never entry is still subject to LRU eviction if its shelf exceeds the size cap (see *Cache eviction*). Purge manually to remove it explicitly.
- **Custom** → free-form entry, accepts `90s`, `45m`, `3h`, `2d` style strings, stored as seconds
- **Bypass** → `ttl_seconds = 0` → explicit "do not cache" carve-out, so you can cache `/jira/rest/**` for 1h but exempt `/jira/rest/api/2/myself`

### query_mode (per rule)

- `include` — query string is part of the cache key (sorted params, so `?a=1&b=2` == `?b=2&a=1`)
- `ignore` — query string stripped from the key; all variants share one entry
- `reject` — only cache requests *without* query params; anything with a query string passes through uncached

This covers "caching with or without query parameters" both ways: keying on them, or collapsing/skipping them.

### Wildcards & matching

- `*` matches within a single path segment (`ABC-*`, `*/issue`)
- `**` matches across segments (rest-of-path)
- Compiled to anchored regexes once, cached in-process; recompiled on rule change (bump a `rules_version` counter checked per request — cheap SQLite read, or invalidate via in-process signal since UI and proxy share the process)

**Most-specific wins** among the request's upstream's rules *(amended 2026-10-14)*, resolved by (in order): number of literal (non-wildcard) characters desc, then fewest wildcards, then longest pattern. Ties broken by newest rule. The UI's "Test a URL" page shows exactly which rule wins and why.

No matching rule → pass through, no caching.

## Cache storage (shelve)

- Directory `_cache/`, one shelf per upstream prefix (`_cache/jira.shelf`, etc.) to keep files small and let you nuke one service's cache without touching others
- `shelve.open(..., writeback=False)`; all access serialized per-shelf via `asyncio.Lock` (shelve/dbm is not concurrency-safe)
- **Lock scope**: the per-shelf lock is held **only around the read and the write** — it is **released during the upstream fetch**. A slow origin call must never stall other requests to the same prefix (including plain cache HITs). Flow is: acquire → read → release → fetch upstream → acquire → write → release.
- **Key**: `sha256(f"{identity}|{path}?{normalized_query}")` — identity is the partition `<name>#<id>` or `anonymous`. Normalized query is sorted `key=value` pairs, or empty per `query_mode`
- **Value** (plain dict, pickled by shelve):

```python
{
	"status": 200,
	"headers": {...},        # filtered: hop-by-hop + Set-Cookie stripped;
	                         # Content-Encoding dropped, Content-Length/Transfer-Encoding
	                         # not stored (recomputed on serve) — see Header hygiene
	"body": b"...",          # stored as httpx delivers it (auto-decoded)
	"content_type": "application/json",
	"fetched_at": 1724900000.0,
	"ttl": 3600,             # None = never expires
	"url": "/jira/rest/api/2/issue/ABC-123?fields=summary",  # human-readable, for the cache browser
	"identity": "jira-uat#3",  # partition: "<name>#<id>" or "anonymous"
	"rule_id": 42,
	"hits": 17,              # periodically-synced snapshot; live count kept in-memory
	"last_accessed": 1724900000.0,  # for LRU eviction; in-memory between flushes
}
```

- Only `200` responses are cached. Everything else passes through.
- Responses over a size cap (default 10 MB, configurable) pass through uncached.

### Hit counting & access tracking (in-memory)

Cache HITs must not incur a shelf write on the hot read path. Instead, an **in-process metadata map** keyed by cache key holds `{hits, last_accessed}`:

- Every serve increments `hits` and updates `last_accessed` **in memory only**.
- A periodic flush writes the map back into the shelf under the per-shelf lock, every `PROXY_FLUSH_SECONDS` (default **60**); a final flush runs on graceful shutdown.
- Stored `hits` is therefore a periodically-synced snapshot; the cache browser displays the last-flushed value merged with the live in-memory delta.
- Counts lag and reset on an ungraceful crash — accepted trade-off.

### Cache eviction (size cap + LRU)

Each shelf has a byte budget, `PROXY_CACHE_MAX_BYTES` (per-shelf, default **500 MB**, confirmed 2026-10-12):

- On write, if the shelf exceeds its budget, evict least-recently-used entries until back under budget.
- LRU ordering uses the in-memory `last_accessed` from *Hit counting*, falling back to the stored `fetched_at` for entries not accessed since the last restart.
- **Never-TTL entries are still evictable** — a full shelf will drop them by LRU. "Never expires" governs TTL only, not eviction.
- **Startup orphan sweep** *(added 2026-10-12)*: during the loading step, every shelf entry whose partition names no live identity is deleted — deleted identities' `<name>#<id>` partitions, the retired shared partition (`""`), and Plan 1's `basic:`/`token:` keys. `anonymous` and every live identity's partition are kept. The cache browser therefore never shows ghosts, and `rm -rf _cache` is no longer the only cleanup.

## Request flow (GET)

Every request, whatever its method, first passes admission: the Host allowlist, identity resolution, and client approval (see *Identities* and *Network trust and client approval*).

1. ~~Resolve upstream from first path segment (miss → 502)~~ Resolve the upstream from `X-WR-Upstream`, or from the identity's single binding (unknown or ambiguous → 400, unbound → 403) *(amended 2026-10-14)*
2. Match path against the upstream's enabled rules → rule or None
3. No rule / bypass rule / `reject` mode with query present → **pass through** (no cache read or write)
4. Build cache key; look up shelf entry
5. **Fresh hit** → serve from cache, add `X-Cache: HIT`, `Age: <seconds>`
6. **Miss or stale** → fetch upstream (see resilience), store on 200, serve with `X-Cache: MISS` (or `REFRESH`)

Non-GET methods always pass straight through (`X-Cache: PASS`) and are never cached — but see *Invalidation on mutation*.

### Invalidation on mutation

After a mutating request (`PUT/POST/PATCH/DELETE`) returns a **2xx** from upstream, purge cached GET entries for the **same prefix + exact normalized path**:

- Purge the **request's own partition** only (its identity, or `anonymous`). Another identity's entry for the same path is untouched and stays bounded by its TTL.
- Purge all query-variant keys for the path (covers `query_mode = ignore` and `include`).
- A **non-2xx** mutation purges nothing (the write didn't take).
- Implemented via `cache.purge_by_path(prefix, path, identity)` in `cache.py`. The mutating response carries `X-Cache-Invalidated: <n>` with the count purged.

**Documented limitation:** only *exact-path* GET entries are invalidated. A collection write (e.g. `POST /jira/rest/api/2/issue`) does **not** invalidate list/search GETs (`/jira/rest/api/2/search?jql=…`) — those remain bounded by their TTL. This is targeted invalidation, not full cache coherence.

## Resilience

- **Timeouts**: per-upstream connect/read timeouts via httpx
- **Retries**: 2 retries with jittered backoff on connect errors / 502/503/504 (GET only — it's idempotent)
- **Stale-if-error**: if upstream fails after retries and a stale entry exists, serve it with `X-Cache: STALE` and `Warning: 110` — this is the big resilience win; Jira going down doesn't take your consumers with it
- **Stampede control**: per-key single-flight — concurrent requests for the same stale/missing key await one upstream fetch instead of dogpiling
- **Header hygiene**: strip hop-by-hop headers both directions; never cache or forward `Set-Cookie` from cached entries; a client's `Authorization` is rejected with `400`, and its `Cookie` is stripped. `Set-Cookie` is stripped from every client response, because the upstream session belongs to the proxy. The upstream httpx clients use a cookie jar that stores nothing, so one identity's upstream session can never be replayed on another identity's request. The upstream `Authorization` is injected from the identity, and `X-WR-Identity` is never forwarded upstream (see *Identities*). **Content-encoding correctness**: httpx auto-decompresses upstream responses, so the cached body is already decoded — drop `Content-Encoding` before caching, do not store `Content-Length` or `Transfer-Encoding`, and recompute `Content-Length` when serving. (Replaying a stale `Content-Encoding: gzip` over a decoded body would make clients gunzip plaintext and fail.)

## Admin UI (`/_admin`)

- **Rules list**: pattern, TTL (human-readable), query mode, enabled toggle, hit counts, edit/delete
- **Copy rules from…**: choose a source upstream and a target upstream. ~~Every rule whose pattern begins with the literal segment `/<source>/` is duplicated with that segment rewritten to `/<target>/`~~ Every rule of the source upstream is duplicated onto the target with the same pattern *(amended 2026-10-14)*, keeping its TTL, query mode, enabled state and note. A pattern that already exists on the target is skipped, and the result lists what was copied and what was skipped. Rules are copied, not linked, so later edits to the source don't follow. Copying touches only the `rules` table, so it has no bearing on identities or approvals.
- **New/edit rule form**: pattern, TTL dropdown (presets + Never + Bypass + Custom), query mode radio, note
- **Test a URL**: choose an upstream (or none, to see what inference picks), paste a path (+ optional query), see the upstream reached or the 400/403 the request would get, the matched rule, computed cache key, current cache state (fresh/stale/absent) *(amended 2026-10-14)*
- **Cache browser**: per-upstream entry list (url, identity, age, ttl, size, hits), filterable by identity, purge single entry, purge by rule, purge by identity, purge all
- **Upstreams**: prefix → base URL CRUD, plus a checkbox per identity that binds it to the upstream *(amended 2026-10-12)*
- **Identities**: name, bound upstreams (read-only; bound on the Upstreams page), scheme, username or header name, token health bar with a **Rotate** link, and a write-only token field (a token can be set or replaced, never shown)
- **Approvals**: pending prompts (answerable here), Always approvals (revocable), and in-memory One hour / Session approvals and No cooldowns
- JSON API mirrors all of it under `/_admin/api/` for scripting

`/_admin` binds with everything else but is exempt from proxying (reserved prefix, underscore keeps it from colliding with real services). It answers loopback clients only (see *Network trust and client approval*).

## Open questions / assumptions made

1. **"Never" interpreted as "never expires"** (not "never cache") — added explicit **Bypass** for the never-cache case. Flag if you meant it the other way.
2. **Auth-varying responses**: RESOLVED — caching is partitioned per identity, where the identity is the `X-WR-Identity` name (see *Identities* and *Per-identity caching*). Tokens live only in the Keychain.
3. **Single process assumed** — shelve + asyncio locks don't survive multiple workers. Run uvicorn with `workers=1`. If throughput ever demands more, the cache layer swaps for sqlite-backed storage, but shelve was the requirement so single-worker it is.
4. Upstream config seeded via env (`PROXY_UPSTREAMS="jira=https://jira.example.com,confluence=..."`) on first run, editable in UI after. "First run" = empty `upstreams` table; seeding is idempotent (skip prefixes that already exist).

### Recorded assumptions (from design review, 2026-09-02)

5. **Trust boundary** *(superseded 2026-10-10)*: the SSO / authenticating-front-layer requirement is gone. Well RESTed is a single-user Jig: loopback is trusted, LAN clients need per-identity approval, and the admin UI is loopback-only (see *Network trust and client approval*).
6. **Invalidation on mutation** is enabled but exact-path only — collection/search GETs are not invalidated by writes and stay TTL-bounded (see *Invalidation on mutation*).
7. **Hit counts are approximate** (in-memory, periodically flushed) and **each shelf is size-capped with LRU eviction** — including Never-TTL entries (see *Hit counting* and *Cache eviction*).
8. **Content negotiation is not part of the cache key** — key is `identity | path ? query` only. Assumes JSON-only REST upstreams (Jira/Confluence); an upstream that varies representation on `Accept` / `Accept-Language` would serve one client's representation to another. Revisit if a non-JSON upstream is added.
9. **Upstream cache directives are intentionally ignored** — `Cache-Control` (incl. `no-store`/`private`), `ETag`, and `Last-Modified` from the origin do not affect caching; rule-driven TTLs are authoritative.
10. **Only `200` is cached** — cacheable `301`/`308`/`404` are deliberately forgone for v1 simplicity.
11. **Range requests**: a cached full-`200` served against a `Range:` request may break strict clients. Known edge case, unhandled in v1.

### Amendment: explicit identities (2026-10-10)

12. **`X-WR-Identity` replaces header-derived identity.** This supersedes Plan 1's identity code (`basic:<username>` / `token:<sha256>` identities and the `upstreams.auth_header` column) and the rule "raw credentials are never persisted", which becomes "tokens never leave the Keychain". It lands before Plan 2's stale-if-error, which depends on it.
13. **The approval prompt needs a GUI login session.** Run as a LaunchDaemon, there is no one to ask, so prompts appear only on the admin approvals page.
14. **Status codes added:** `400` for a client `Authorization`, an unknown identity, ~~an identity/prefix mismatch~~ *(a `403` since 2026-10-14)*, or a Host off the allowlist; `403` for a denied approval or a non-loopback `/_admin` request; `503` + `Retry-After` while an approval is pending; `503` when an identity's token is missing from the Keychain.

### Amendment: no shared cache entries (2026-10-11)

15. **The per-rule `shared` flag is removed.** It existed so that a main user and a service account could share one cached response. With identities held at the proxy, it became the only way a caller could read a response fetched with a token it was never approved for: an anonymous LAN client, even one answered **No**, could read a shared entry. Every entry now belongs to exactly one partition.
16. **Cost accepted:** each identity fetches and caches its own copy of every response, including ones that are the same for everybody.
17. **Schema v3** drops `rules.shared`. A rule that had `shared = 1` becomes an ordinary per-identity rule. Entries already stored under the shared partition (`""`) are unreachable from then on. LRU eviction (Plan 2) removes them later, and `rm -rf _cache` clears them at once.
18. **"Copy rules from…" is the convenience that replaces it** (Plan 3, see *Admin UI*). Rules are duplicated from one upstream prefix to another; cached responses are never shared.
19. **Keychain read failures and CLI name casing** *(found in the Plan 1.5 review)*: a failed Keychain read is treated as a missing token instead of an error, and the CLI always uses an identity's stored spelling (see *Identities*).

### Amendment: Plans 2 and 3 combined (2026-10-12)

20. **One plan, two phases.** Plan 2 (resilience and freshness) and Plan 3 (admin UI) are executed as a single combined plan — resilience first, because the UI displays its outputs (hit counts, STALE state, what eviction leaves behind).
21. **`PROXY_CACHE_MAX_BYTES` defaults to 500 MB per shelf** (confirmed) and **`PROXY_FLUSH_SECONDS` defaults to 60** for the hit-count/LRU metadata flush. Both are env-overridable.
22. **Startup orphan sweep** (see *Cache eviction*): unreachable partitions are deleted during the loading step rather than waiting for LRU.
23. **HTMX is vendored** into `static/`, pinned, no CDN.
24. **Approvals: first answer wins** when a pending prompt is answerable both on screen and on the approvals page (see *Network trust and client approval*).

### Amendment: identities span upstreams (2026-10-12)

25. **An identity is bound to any number of upstreams, and an upstream to any number of identities.** One Atlassian account (an email plus an API token) reaches many sites, so the identity is the credential and the upstreams are where it may be sent. This supersedes "an identity belongs to exactly one upstream".
26. **Identity first, then upstream.** An identity is created with no upstreams. Bindings are edited on the Upstreams page (a checkbox per identity on the add and edit forms) and in its JSON mirror (`identities: [names]`; omitted on an edit means unchanged). The Identities page and `identity list` only show them. The CLI has no binding command, because there is no upstream CLI.
27. **Schema v4** adds `identity_upstreams (identity_id → identities ON DELETE CASCADE, prefix → upstreams ON DELETE CASCADE, PRIMARY KEY (identity_id, prefix))` and drops `identities.prefix`, carrying each existing identity's prefix over as its first binding. The rebuild keeps identity ids and the AUTOINCREMENT high-water mark (so partitions and approvals survive, and a deleted id is still never reused). It also keeps `approvals` intact: dropping the old table under `foreign_keys=ON` would otherwise cascade-delete them.
28. **Admission:** a request naming an identity that isn't bound to the request's ~~prefix gets a `400`~~ upstream gets a `403` *(amended 2026-10-14, item 47)*. A binding change bumps `identities_version`, so it reaches the next request.
29. **Approvals stay keyed by `(client address, identity)`.** An approval covers every upstream the identity is bound to, including upstreams bound later. The prompt still names the upstream that triggered it.
30. **Deleting an upstream unbinds its identities** instead of being refused. The identities themselves, and their tokens and approvals, are untouched.
31. **Cache partitions are unchanged** (`<name>#<id>` within the upstream's own shelf). The startup orphan sweep treats a partition as live on a shelf only while that identity is bound to that shelf's prefix, so entries stranded by an unbinding are removed at the next start.
32. ~~**`favicon.ico` is a reserved prefix.**~~ *(Since 2026-10-14 upstream ids are not paths, so `favicon.ico` is an ordinary id; the app still answers the `/favicon.ico` path itself.)* The app answers `/favicon.ico` itself with the admin UI's icon, because browsers request it unprompted and it used to fall through to the catch-all as an unknown prefix (a `502`).

### Amendment: default port, and outbound proxies (2026-10-13)

33. **The default port is 1337** (`PROXY_PORT` still overrides it). 8080 collides with too much.
34. **Named outbound proxies, at most one per upstream.** `proxies (name PK [a-z0-9-], url, username, verify_tls, ca_bundle)` and `upstreams.proxy → proxies(name) ON DELETE RESTRICT` (NULL = Direct), schema v5, with a `proxies_version` counter bumped by every proxy write. Names are immutable. Out of scope: chaining, per-identity routing, PAC, NO_PROXY lists.
35. **Schemes are whatever `httpx.Proxy` accepts** (http, https, socks5, socks5h; SOCKS needs `socksio` via `httpx[socks]`). A URL with userinfo is refused at save. SOCKS URLs need an explicit port.
36. **Proxy passwords live in the Keychain** (service `well-rested-proxy`, account = proxy name), loaded at startup and on every `proxies_version` change. A blank password on edit keeps the stored one. Username set but no item → `503` naming the proxy. A proxy that can't be used never falls back to Direct.
37. **`trust_env=False` on every upstream client.** Shell `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` and `SSL_CERT_FILE`/`SSL_CERT_DIR` are ignored. Trust is certifi, plus a proxy's `ca_bundle` for the proxy itself and for every upstream routed through it (for TLS-intercepting proxies).
38. **Clients are pooled per route** `(verify_tls, proxy, generation)`. Supersedes the brief's per-prefix clients: the code already pooled by setting rather than by upstream. A proxy edit or delete retires that generation's clients; they finish in-flight requests and are then closed.
39. **The route is not part of the cache key,** and changing it purges nothing.
40. **Proxy failures are the proxy's:** connect failure/timeout, CONNECT or SOCKS refusal, and a 407 on a forwarded `http://` request → `502` naming the proxy; missing credentials or an unusable CA bundle → `503`. All count as upstream failures for stale-if-error.
41. **Plaintext is a warning, not a refusal:** an `http://` upstream with a proxy and a bound identity shows ⚠️ on the Upstreams row, because the proxy can read the injected `Authorization`.
42. **No per-proxy Test button;** Test a URL shows the route and stays read-only.

### Amendment: a custom identity scheme (2026-10-13)

43. **A third scheme, `custom`, for upstreams that want something other than Basic or Bearer** (e.g. `Authorization: Token abc123`). The identity's Keychain secret is the whole header value, scheme word included, and it is injected verbatim. A custom identity takes no username. Its value keeps inner spaces but may not contain control characters, so it can never break the header. Token health, rotation, 401 → rejected and the client-`Authorization` refusal work exactly as for the other schemes. ~~Custom header *names* (e.g. `X-API-Key`) are out of scope: injection stays on `Authorization`.~~ *(Superseded 2026-10-16 by item 56.)*
44. **Schema v6** rebuilds `identities` to widen its `scheme` CHECK to `('basic', 'bearer', 'custom')`. As in v4, `identity_upstreams` and `approvals` are set aside first (both cascade off `identities` under `foreign_keys=ON`), and identity ids and the AUTOINCREMENT high-water mark are kept, so cache partitions, bindings and approvals survive.

### Amendment: header routing (2026-10-14)

45. **`X-WR-Upstream` selects the upstream, and the path is forwarded untouched.** The base URL is plain `http://localhost:1337`. `/rest/api/2/myself` with `X-WR-Upstream: jira` goes to the jira base URL plus `/rest/api/2/myself`; a base URL with a path keeps it (`https://h/jira` + `/rest/x`). This is a clean break: there is no path-prefix fallback, and `/jira/rest/x` is now simply that path on whichever upstream the request names. This supersedes *Routing to upstreams* and request-flow step 1.
46. **Resolution order.** A client `Authorization` → `400`. A repeated or empty `X-WR-Upstream` or `X-WR-Identity` → `400`. An unknown identity → `400`. With `X-WR-Upstream`: it matches an upstream id in any letter case (ids are already unique case-insensitively), an unknown one → `400` listing the configured upstreams, and one the identity isn't bound to → `403`. Without it: an identity bound to exactly one upstream uses that one, one bound to several → `400` listing them, one bound to none → `403`, and an anonymous request → `400` listing the configured upstreams. Then the Keychain and approval checks run as before. `X-WR-Upstream` is never forwarded upstream.
47. **An unbound upstream is a `403`, no longer a `400`.** The brief asked for a `403` "as for an unbound identity today"; the unbound identity had in fact been a `400` (item 28). The two are now one check, and both are `403`: the request is well-formed, but that identity may not use that upstream.
48. **Anonymous requests.** There is no shared-rule flag (item 15), but every rule serves the `anonymous` partition, so a request with no `X-WR-Identity` and an `X-WR-Upstream` (for example `/rest/api/2/serverInfo`) is routed and cached as anonymous. It can't infer its upstream, so the header is required. No approval is needed, as before, because no credentials are injected; this is not a conflict with the approval or binding model, since both exist only for injected credentials. It does mean, as before, that any LAN client can make unauthenticated requests to any configured upstream.
49. **`prefix` is renamed `upstream_id`** in the schema (v7: `RENAME COLUMN` on `upstreams` and `identity_upstreams`; SQLite rewrites the references, so nothing is rebuilt), the code, the JSON API (`"upstream_id": "jira"`, routes `/_admin/api/upstreams/{upstream_id}`) and the UI. The id keeps its old rules (letters, digits, `.`, `_`, `-`, no leading `_`), because it still names the shelf file. `PROXY_UPSTREAMS` keeps its `id=url` format.
50. **Rules belong to one upstream** (schema v8). `rules` is rebuilt with `upstream_id NOT NULL REFERENCES upstreams(upstream_id) ON DELETE CASCADE` and `UNIQUE (upstream_id, pattern)`; matching only considers the request's upstream's rules. The migration moves every rule whose pattern began with a literal `/<upstream_id>/` (or was exactly `/<upstream_id>`) onto that upstream with the segment removed (`/jira/rest/**` → jira `/rest/**`; `/jira` → jira `/`; `/jira` and `/jira/` collide, and the older rule wins). Rules whose first segment wasn't a literal, existing upstream id (e.g. `/**`, `/*/rest/**`) can't be placed and are dropped; r's database had no rules when this shipped. Ids and the AUTOINCREMENT high-water mark are kept, because cache entries carry `rule_id`. Deleting an upstream now deletes its rules (bumping `rules_version`). *Copy rules from…* copies rows with their patterns unchanged; *Test a URL* takes an upstream.
51. **`Location` is rewritten; bodies are not.** On every response served (live, HIT and STALE), a `Location` on the upstream's scheme, host and effective port, under the base URL's path at a segment boundary, becomes the proxy's base URL (scheme and the caller's `Host`, already allowlisted) plus the rest; a path-absolute `Location` under a base URL's path loses that path, and one outside it is made absolute to the upstream (the only place it resolves). A rewritten value is never left scheme-relative: a rest of `//host` or `/\host` (an upstream echoing `/jira//evil.com` into a trailing-slash redirect) always gets the proxy's base URL in front, so the proxy can't be made an open redirect. It applies to any status, so a `201 Created` is covered as well as redirects. A `Location` to any other host (an SSO login) is untouched, as are `Link`, `Content-Location` and every body: JSON `self` URLs still name the upstream.
52. **The old cache is cleared.** Entries keyed on `/<prefix>/…` paths can never be hit again. The cache directory now holds a `LAYOUT` marker (`2`); at startup, a directory without it has its shelf files (`<upstream_id>.shelf` plus the dbm suffixes, nothing else) deleted before any shelf opens, and the marker is written. A second instance started by mistake during the upgrade could clear a running server's shelves before failing to bind; it's a one-off at upgrade time and the cost is a cold cache, so there is no lock.
53. **Unreachable upstream paths.** `/_…` paths (the admin UI, loopback-only) and `/favicon.ico` are answered by the proxy itself, so an upstream's own paths there can't be reached through it.
54. **Upstream ids from the outside are checked before they become paths** (found in this change's review, but older than it). The cache store refuses any shelf name that isn't an upstream id, and so do the admin cache handlers (`400`), so `?upstream_id=../../x` can no longer open or purge a file outside the cache directory. Seeding skips an id that differs from an earlier one only in case, and `X-WR-Upstream` prefers an exact spelling should two such rows exist. The upstream URL refuses a path that doesn't start with `/`.
55. **Schema numbering.** v7 and v8 are taken by this change; the per-upstream CA bundle in `docs/todo.md` moves to v9 *(then to v10, item 58)*.

### Amendment: custom header names (2026-10-16)

56. **A custom identity names the header it injects under.** `identities.header` (custom only; NULL for basic and bearer, which stay on `Authorization`) holds the name, e.g. `x-api-key` for Claude's API or `Cookie` for a site that authenticates with a session cookie. It is not a secret: it shows on the Identities page, in `GET /_admin/api/identities` (`"header"`) and in `identity list`, and is typed as `--header` on the CLI or in the add form's Header field; blank means `Authorization`, so every existing custom identity and every old command line keep their meaning. The name is fixed at creation, like a basic username; rotation replaces the value only. The value is still sent verbatim and still refuses control characters. A name must be an RFC 7230 token of at most 64 characters, and may not be one the proxy sets itself (`Host`, `Content-Length`, `Accept-Encoding`, `X-WR-Identity`, `X-WR-Upstream`, or a hop-by-hop header such as `Connection` or `Proxy-Authorization`); `Authorization` and `Cookie` are allowed, since they are the point. At request time the client's own header of that name (any letter case) is dropped before the identity's is appended, so the upstream never sees two, and the client-`Authorization` → `400` is unchanged. This supersedes the last sentence of item 43. Also found here: a token with a character outside latin-1 used to pass validation and fail with `UnicodeEncodeError` at send time; every scheme now refuses it at save.
57. **Schema v9** is a plain `ALTER TABLE identities ADD COLUMN header TEXT` followed by `UPDATE identities SET header = 'Authorization' WHERE scheme = 'custom'`. No rebuild, so ids, bindings and approvals are untouched.
58. **Schema numbering.** v9 is taken by this change; the per-upstream CA bundle in `docs/todo.md` moves to v10.

### Amendment: platform support (2026-10-22)

59. **macOS only, for now.** Secrets live only in the macOS Keychain (`proxy/keychain.py`), and the LAN approval dialog is `osascript`. On any other platform, `KeychainStore()` raises at startup, so the proxy and the CLI refuse to run rather than fall back to a weaker store. The README says so up front.
60. **Windows will use Windows Credential Manager.** That's the per-user, DPAPI-protected vault, reached through `keyring`'s `WinVaultKeyring` backend behind the existing `SecretStore` protocol and picked by platform, never through `keyring.get_keyring()`. A plaintext backend, environment variables, a `.env` file or WSL2 is not an acceptable stand-in, because each one puts tokens outside an OS-protected store. The shape is in `docs/todo.md` (*Windows support*).
