# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Well RESTed: a local, single-process, GET-only caching REST proxy (FastAPI + SQLite + `shelve`) for macOS. Clients call `http://localhost:1337/<upstream path>`, name the upstream with `X-WR-Upstream` and an identity with `X-WR-Identity`; the proxy injects that identity's upstream credentials from the macOS Keychain. A loopback-only admin UI lives at `/_admin` (Jinja2 + vendored HTMX, with a JSON mirror under `/_admin/api/`). `docs/cache-proxy-design.md` is the spec, and its amendment sections at the end override earlier text. `project-index.md` maps every file.

## Commands

It's a "Jig": plain `python3` from the repo root, packages installed with `python3 -m pip install --user`, **no venv**. Dependencies are listed in `pyproject.toml` (`[dev]` adds pytest and pytest-asyncio).

```sh
python3 -m pytest -q                                   # full suite (~8 s)
python3 -m pytest tests/test_stale.py -q               # one file
python3 -m pytest tests/test_caching.py::test_miss_then_hit -q  # one test
WR_TEST_KEYCHAIN=1 python3 -m pytest -m keychain       # opt-in test against the real Keychain

PROXY_UPSTREAMS=jira=https://jira.example.com python3 -m proxy   # run the server (listens on 1337)
python3 -m proxy identity add|rotate|list|delete …     # CLI (proxy/cli.py)
python3 -m proxy approval list|revoke …
```

There is no linter or formatter. Configuration comes from `PROXY_*` env vars (`proxy/config.py`). `PROXY_UPSTREAMS` seeds upstreams on the first run only; after that they live in SQLite. Runtime state is `proxy.db` and `_cache/`, both gitignored.

## Architecture

**App wiring** (`proxy/main.py`). `create_app(settings, transport, secrets, prompter, proxy_secrets)` takes optional replacements for the network (httpx transport), the Keychain (`SecretStore`; `proxy_secrets` is the second store, for outbound-proxy passwords) and the macOS dialog (`Prompter`); tests pass fakes through these. The lifespan puts every shared object on `app.state`: `db`, `rules` (RuleCache), `identities`, `secrets`, `proxies` (OutboundProxies), `proxy_secrets`, `approvals`, `cache`, `flights`, `upstream`. It also runs the orphan sweep at startup, the periodic metadata flush, and a final flush at shutdown. The `/_admin` router must be mounted **before** the proxy catch-all because the first match wins. `HostGuard` middleware enforces the Host allowlist and keeps every `/_…` path loopback-only.

**Single worker by design.** Shelve is not concurrency-safe, and all coordination uses in-process asyncio locks and maps, so uvicorn always runs with `workers=1`. SQLite runs on the event-loop thread only. Only Keychain calls go through `asyncio.to_thread`.

**Request flow** (`proxy/proxy_router.py`):
1. The path is forwarded untouched (only `/_…` is reserved). There is no path prefix.
2. `_admit` runs before any cache access, so HITs are gated exactly like MISSes:
   - a client `Authorization` header gets a 400;
   - `X-WR-Identity` resolves to an identity that has a token;
   - `choose_upstream` (shared with Test a URL) picks the upstream: `X-WR-Upstream` (any case; unknown → 400), else the identity's only binding (several → 400, none → 403); anonymous must send the header; an identity not bound to the named upstream → 403;
   - a non-loopback client needs an approval (`ApprovalGate`).
3. Non-GET requests pass through. A 2xx mutation purges the caller's own entries for that exact path and adds `X-Cache-Invalidated`.
4. A GET with no matching rule, a bypass rule (`ttl 0`), a `reject`-mode rule with a query, or a body also passes through.
5. Any other GET is served from the caller's partition: the identity's `<name>#<id>` or `anonymous`. **Nothing is ever served across partitions.** That includes stale-if-error.
6. Rules belong to one upstream (`RuleSet.match(upstream_id, path)`). A `Location` pointing at the upstream is rewritten to the proxy at serve time (`headers.rewrite_location`); bodies never are.

**Cache** (`proxy/cache.py`):
- There is one shelf per upstream (`<upstream_id>.shelf`), and each shelf has its own asyncio lock. A `LAYOUT` marker in the cache dir lets a key-layout change clear old shelves at startup.
- The lock is held only around a single read or write, never across an upstream fetch.
- Cached-GET fetches run inside `SingleFlight` (`proxy/flight.py`), keyed by `(upstream_id, key)`, so concurrent misses share one upstream call.
- Hits and `last_accessed` live in an in-memory map (`_live`). A HIT never writes the shelf. `flush_meta()` merges the map into the shelf periodically and at shutdown.
- Each shelf gets LRU eviction once it passes `PROXY_CACHE_MAX_BYTES`.
- On this Python, shelve uses `dbm.sqlite3`, so the files are `<upstream_id>.shelf` plus `-wal`/`-shm`.

**Change propagation via version counters.** `db.py` bumps `rules_version` / `identities_version` / `proxies_version` in the same transaction as each write. `RuleCache` and `IdentityRegistry.refresh()` check these counters on every request and reload when they change. That's how edits from the admin UI or the CLI (a separate process) reach the next request. Any new write path must bump the matching counter. Identity ↔ upstream bindings (`identity_upstreams`, many-to-many) count as identity changes. They are written only through `db.set_upstream_identities` / `put_upstream(identities=…)`, which bump `identities_version` only when the set actually changes, because every bump makes the running proxy re-read every token from the Keychain. An upstream is identified by its `upstream_id` (formerly "prefix") everywhere; deleting it cascades to its rules and bindings. A `proxies_version` change reloads the `OutboundProxies` registry (and every proxy password from the Keychain). Upstream clients are pooled per route, `(verify_tls, proxy, generation)`, and a proxies reload retires the old generation's clients: they finish in-flight requests, then close. `trust_env=False` everywhere, so shell proxy and `SSL_CERT_*` variables are ignored.

**Schema** (`proxy/db.py`). Versioned via `PRAGMA user_version`. Migrations form an append-only list: never edit a shipped entry, append a new one, and add a migration test in `tests/test_migrations.py`.

**Identities and tokens** (`proxy/identities.py`, `proxy/keychain.py`):
- Tokens live only in the Keychain. They are never logged, never returned by any admin page or API, and never echoed back on a refused form.
- `IdentityRegistry.credential(identity)` is the only injection path: it returns the `(header, value)` pair, `Authorization` for basic and bearer, and the identity's own `header` column for custom (`x-api-key`, `Cookie`, …; `Authorization` by default). `_fetch` drops a client header of that name before appending it. Allowed names are checked in `db.check_identity` against `headers.RESERVED_REQUEST_HEADERS`.
- `add_identity` / `rotate_token` / `remove_identity` are shared by the CLI and the admin UI. They encode the Keychain-vs-DB ordering rules, so reuse them rather than calling `db`/`keychain` directly.
- `KeychainStore` uses the keyring macOS backend only.
- Outbound proxies (`proxy/outbound.py`): `add_proxy` / `edit_proxy` / `remove_proxy` encode the Keychain-vs-DB ordering, so reuse them. Proxy passwords (service `well-rested-proxy`, account = proxy name) follow the same never-logged, never-shown, never-echoed rules as tokens. A proxy that can't be used never falls back to Direct.

**Admin UI** (`proxy/admin_router.py`, `proxy/templates/`):
- Every handler is `async def`. Each page has a JSON mirror under `/_admin/api/`.
- Forms follow POST → redirect → GET, with the result passed as a `?m=` flash. A refused form re-renders with a 4xx status.
- HTMX partials swap error responses too (configured in `base.html`).
- Confirm dialogs read their text from `data-confirm`. Never interpolate user data into inline JS.
- No CDN references anywhere. `htmx.min.js` is vendored and pinned (see `proxy/static/README.md`).

## Conventions

- Indent with **tabs** in Python, templates and TOML.
- Logging: `log = get_logger(__name__, "TAG")` from `proxy/log.py`. Messages look like `"what happened; key=value key=value"`. Log derived identity names, never header or token values.
- Tests (`tests/conftest.py`):
  - The `make_client` / `client` fixtures start the real app, including its lifespan, with `FakeUpstream` (an httpx.MockTransport), `MemorySecretStore` and `FakePrompter`.
  - `ClientAddr` sets the peer address so tests can simulate LAN clients.
  - Retry backoff is zeroed in the app fixture.
  - Several test files import `shift_clock` from `tests.test_caching`, which relies on running pytest from the repo root.
- Work is done task by task, test-first, with one commit per task. Keep `project-index.md` current when adding, moving or removing files.
- Commit subjects are past tense with a type prefix: `added: …`, `changed: …`, `fixed: …`.
- Anything that needs the real Keychain, the GUI approval dialog or a second LAN machine is a manual smoke step left for r.
