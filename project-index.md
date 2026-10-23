# well-rested — Project Index

> Auto-maintained by Claude. Last updated: 2026-10-22
> About: Local GET-only caching REST proxy (FastAPI + SQLite + shelve) with explicit X-WR-Identity identities, Keychain-held tokens, Host allowlist and LAN client approvals, GET retries, single-flight, stale-if-error, invalidation on mutation, in-memory hit counting, LRU eviction, and a loopback-only `/_admin` UI (Jinja2 + vendored HTMX) with a JSON API mirror; Plans 1, 1.5 and 2+3 built, plus identities bound to many upstreams (schema v4) and named outbound proxies per upstream (schema v5, Keychain-held proxy passwords, default port 1337), a custom identity scheme that sends a raw Authorization value (schema v6) under any header name it chooses, e.g. `x-api-key` or `Cookie` (schema v9, 2026-10-16), and header routing: `X-WR-Upstream` picks the upstream, the path is forwarded untouched, `prefix` became `upstream_id` (schema v7), rules belong to one upstream (schema v8), and `Location` is rewritten to the proxy (as of 2026-10-14)

## Project Structure

### / (Root)
- `project-index.md` — This file
- `README.md` — Readme: platform support (macOS only; Windows not ready), quick start, usage (`X-WR-Upstream` / `X-WR-Identity`), configuration, admin UI, CLI
- `LICENSE` — Mozilla Public License 2.0 (every `.py` under `proxy/` and `tests/` carries the MPL header)
- `.gitignore` — Ignores `_cache/`, `proxy.db` (+ WAL files), Python build/venv artifacts, `*.egg-info/`
- `assets/sleepy-bear.png` — Source image for the favicon (downscaled into `proxy/static/favicon.png`)
- `pyproject.toml` — Package metadata, dependencies (fastapi, uvicorn, httpx, keyring, jinja2, python-multipart), pytest config

### /docs/
Design documentation for the cache-proxy system.

- `docs/cache-proxy-design.md` — Amended design for a GET-only pass-through REST cache proxy (FastAPI + SQLite + shelve, HTMX admin UI) with explicit identities, token health and client approvals
- `docs/cache-proxy-design-review.md` — 2026-09-02 design review: findings, decisions, and resulting spec amendments
- `docs/todo.md` — Agreed but unscheduled work, each with its why and intended shape (a per-upstream CA bundle, now schema v10; Windows support via Windows Credential Manager)

### /proxy/
The cache proxy service (single FastAPI process).

- `proxy/__init__.py` — Package marker
- `proxy/__main__.py` — `python -m proxy` runs uvicorn (workers=1); `identity`/`approval` args dispatch to the CLI
- `proxy/main.py` — App factory; serves `/favicon.ico`; lifespan wires db, rule cache, identities, outbound proxies (`app.state.proxies`, second Keychain store `proxy_secrets`), approvals, cache store (plus startup orphan sweep and the periodic/final metadata flush), single-flight, upstream client; mounts `/_admin` before the proxy catch-all
- `proxy/config.py` — Settings from PROXY_* env vars (incl. allowed hosts, approval timings, `PROXY_FLUSH_SECONDS`, `PROXY_CACHE_MAX_BYTES`); upstream seed parsing
- `proxy/log.py` — Central bracket-tagged logger with tag allowlist and level floor
- `proxy/rules.py` — Rule dataclass (one upstream each), wildcard compilation, most-specific-wins RuleSet grouped by upstream (`match(upstream_id, path)` and the ranked `match_all`)
- `proxy/db.py` — Versioned SQLite schema with migrations; upstream keyed by `upstream_id` (incl. delete that unbinds and drops its rules, case-collision guard, case-insensitive `resolve_upstream`), identity ↔ upstream bindings, per-upstream rule (full CRUD, copy_rules), identity and approval CRUD (schemes basic, bearer, custom; a custom identity's `header` name, validated against the reserved request headers); schema v5 (proxies table, `upstreams.proxy` ON DELETE RESTRICT, proxy CRUD); schema v6 (identities rebuilt to allow `custom`); schema v7 (`prefix` → `upstream_id`); schema v8 (rules rebuilt with `upstream_id`, unique per upstream); schema v9 (`identities.header`); version counters (rules, identities, proxies)
- `proxy/keychain.py` — SecretStore protocol; macOS Keychain store (keyring macOS backend only; `PROXY_SERVICE` for outbound-proxy passwords) and in-memory test store
- `proxy/outbound.py` — Outbound proxies: URL and CA-bundle validation, `tls_context`, `Route`, `OutboundProxyFailure`, `add_proxy`/`edit_proxy`/`remove_proxy` (Keychain ordering rules) and the `OutboundProxies` registry
- `proxy/identities.py` — IdentityRegistry: Keychain loading step, resolve, credential injection as a `(header, value)` pair (basic, bearer, or a custom value sent verbatim under the identity's own header), scheme-aware token validation (latin-1 only), token health and Warning text; `parse_when`; the add/rotate/delete helpers (Keychain ordering rules) shared by the CLI and the admin UI
- `proxy/admission.py` — HostGuard ASGI middleware: Host allowlist, loopback-only `/_…` paths, address helpers
- `proxy/approvals.py` — ApprovalGate (Always/One hour/Session/No, single-flight, serialized prompts; UI answers race and close the dialog; snapshot/revoke/clear for the admin page) and OsascriptPrompter
- `proxy/headers.py` — Hop-by-hop and encoding hygiene; drops Authorization/Cookie/X-WR-Identity/X-WR-Upstream upstream and Set-Cookie downstream; `RESERVED_REQUEST_HEADERS` (names a custom identity may not inject under); `rewrite_location` points an upstream `Location` back at the proxy
- `proxy/cache.py` — Query normalization, cache keys, the ANONYMOUS partition, per-upstream shelve CacheStore (with a `LAYOUT` marker that clears shelves of an older key layout) with in-memory hit/access metadata (flushed), per-shelf LRU eviction, targeted purges, browse, orphan sweep
- `proxy/flight.py` — SingleFlight: concurrent callers for one key share one shielded execution
- `proxy/upstream.py` — httpx clients pooled per route `(verify_tls, proxy, generation)` with `trust_env=False`, a non-storing cookie jar, retire/drain on proxy edits and proxy-failure error mapping; GET retries with jittered backoff on connect errors and 502/503/504; base_url + the untouched path
- `proxy/proxy_router.py` — Catch-all route: `choose_upstream` (X-WR-Upstream or the identity's only binding; 400/403 refusals, shared with Test a URL), identity admission, credential injection, `Location` rewrite at serve time, pass-through (with invalidation on 2xx mutations) and the partitioned cached GET flow (single-flight, stale-if-error, hit recording); `_fetch` resolves the route and `_upstream_error` maps `OutboundProxyFailure` to 502/503
- `proxy/admin_router.py` — `/_admin` pages and `/_admin/api/` JSON mirror: upstreams (+identity binding checkboxes, proxy choice and route column with plaintext warning), proxies (write-only passwords), rules (per upstream, filter, +copy, toggle), test a URL (upstream select; shows the upstream reached or the refusal, and the route), cache browser and purges, identities (write-only tokens, header name for custom), approvals
- `proxy/templates/` — Jinja2 pages (`base.html` + one per page, row/partials for HTMX swaps); tabs, no CDN
- `proxy/templates/proxies.html` — Proxies page: add form and per-row edit/delete for named outbound proxies
- `proxy/static/` — Vendored `htmx.min.js` (pinned; see its README.md), our `style.css` (incl. `.warn`, `button.copy`, `[hidden]`) and `admin.js` (data-attribute driven: scheme-only fields on the identity form, `data-copy` buttons), and `favicon.png` (also served at `/favicon.ico`)
- `proxy/cli.py` — `identity add|rotate|list|delete` and `approval list|revoke` on the shared identity helpers; `--header` for custom identities; tokens via getpass or --token-stdin

### /tests/
Pytest suite; upstreams faked with httpx.MockTransport.

- `tests/conftest.py` — FakeUpstream, ClientAddr shim, FakePrompter, MemorySecretStore, `proxy_secrets` and add_identity/add_proxy fixtures
- `tests/test_passthrough.py` — Pass-through, header hygiene, error mapping, raw-path forwarding
- `tests/test_header_routing.py` — X-WR-Upstream resolution and inference, 400/403 outcomes, anonymous routing, path forwarded untouched, `Location` rewriting end to end
- `tests/test_caching.py` — HIT/MISS/REFRESH/PASS, query modes, per-user partitioning
- `tests/test_concurrency.py` — Shelf lock not held across upstream fetch; concurrent misses share one upstream fetch
- `tests/test_retries.py` — GET-only retries, backoff + jitter, what is and isn't retried
- `tests/test_flight.py` — SingleFlight primitive: sharing, failure propagation, waiter cancellation
- `tests/test_stale.py` — Stale-if-error on connect errors and final 5xx; never across partitions; Warning headers
- `tests/test_invalidation.py` — 2xx mutations purge the caller's partition for the exact path; `X-Cache-Invalidated`
- `tests/test_meta.py` — HITs never write the shelf; flush merges hits/last_accessed; periodic and final flush; `hits_by_rule`
- `tests/test_eviction.py` — LRU eviction under a byte budget, totals consistency, the startup orphan sweep
- `tests/test_admin_scaffold.py` / `test_admin_upstreams.py` / `test_admin_rules.py` / `test_admin_copy.py` / `test_admin_testurl.py` / `test_admin_cache.py` / `test_admin_identities.py` / `test_admin_approvals.py` — One file per admin page group: pages and JSON mirror, refusals, loopback-only, token-free responses, version-counter propagation
- `tests/test_db_proxies.py` — Schema v5 proxies table, upstream.proxy RESTRICT, proxy CRUD, `proxies_version` bumps
- `tests/test_outbound.py` — Proxy URL and CA-bundle validation, add/edit/remove ordering with the Keychain, registry reload
- `tests/test_outbound_live.py` — Real loopback-socket proxies: forwarding, CONNECT, refusals, 407
- `tests/test_outbound_routing.py` — Route resolution, per-route client pooling and retirement, 502/503 mapping, stale-if-error through a proxy
- `tests/test_admin_proxies.py` — Proxies page and JSON mirror, upstream proxy choice, password never shown, route in Test a URL
- `tests/test_main_entry.py` — `python -m proxy` runs uvicorn single-worker; CLI arguments dispatch to `proxy.cli`
- `tests/test_migrations.py` — Fresh v9 schema, in-place migration of v1–v8 databases (v3 → v4 keeps ids, approvals and the AUTOINCREMENT mark; v6 → v7 renames to `upstream_id`; v7 → v8 places path-prefixed rules on their upstream; v8 → v9 backfills `header` for custom identities), newer-schema refusal, UPSERT under foreign keys
- `tests/test_identities_db.py` — Identity/approval CRUD, name, header-name and field validation, identity ↔ upstream bindings, version bumps, cascade deletes
- `tests/test_keychain.py` — Memory store round trip; KeychainStore never consults keyring.get_keyring(); opt-in real Keychain test
- `tests/test_identities.py` — Health levels, Warning texts, token validation, registry loading/reload/status recording
- `tests/test_admission.py` — Host normalization and allowlist, loopback detection, reserved paths, LAN-bind warning
- `tests/test_approvals.py` — Gate decisions, cooldown, single-flight, serialized prompts; osascript argv safety and timeout
- `tests/test_wiring.py` — Startup loading step reads each token once and survives a missing token
- `tests/test_identity_routing.py` — Injection (Authorization, and a custom identity's own header replacing the client's), 400/403/503 admission outcomes, one identity across several upstreams, unbinding, partitioning, rotation, approvals, Warning headers
- `tests/test_cli.py` — parse_when, add/rotate/list/delete, approvals, battery rendering
- `tests/test_rules.py` / `test_db.py` / `test_cache.py` / `test_headers.py` / `test_upstream.py` / `test_config.py` / `test_log.py` — Unit tests per module
