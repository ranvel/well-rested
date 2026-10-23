```
z
          Z
       _         _
      ( )_.-""-._( )
       /         \
      |  (-) (-)  |
      |   ( _ )   |
      |   '---'   |
       \         /
        '-------'
```

# Well RESTed

A local caching proxy for REST APIs (Jira, Confluence, anything over HTTP) that runs on your Mac.

- **Caching:** GET responses are cached according to rules you write. Every other method passes straight through.
- **Routing:** clients call `http://localhost:1337` with the upstream's own path, and name the upstream in an `X-WR-Upstream` header (optional when their identity is bound to just one upstream).
- **Credentials:** clients never hold credentials. They name an identity in an `X-WR-Identity` header, and the proxy adds that identity's token from the macOS Keychain.
- **Outages:** when an upstream goes down, callers get their last good response marked as stale, not an error.
- **Admin UI:** a loopback-only web page at `/_admin` manages upstreams, rules, the cache, identities, outbound proxies and LAN client approvals.

The full design is in [`docs/cache-proxy-design.md`](docs/cache-proxy-design.md).

> **macOS only, for now.** Well RESTed doesn't run on Windows yet: it has no place there to store credentials securely. See [Platform support](#platform-support).

## Platform support

| Platform | Status |
|----------|--------|
| **macOS** | Supported. This is the only platform it's built and tested on. |
| **Windows** | **Not ready.** The proxy and the CLI both refuse to start (`the macOS Keychain is not available on this system`). |
| **Linux** | Not ready, for the same reason. |

Three things tie it to macOS:

- **Secret storage.** Identity tokens and outbound-proxy passwords live only in the macOS Keychain (`proxy/keychain.py`). This is deliberate. A token is never written to the database, the cache, a log or a config file, so it can't leak from any of them. With no Keychain, there's nowhere safe to keep a token, so nothing starts.
- **The LAN approval dialog.** It's a native macOS dialog shown through `osascript`. The Approvals page in the admin UI can answer the same prompts, but nothing has been tested without the dialog.
- **Some doc examples** use macOS commands, such as `security find-generic-password` to check a Keychain item.

**What Windows needs.** The Windows equivalent of the Keychain is **Windows Credential Manager**. It's the per-user credential vault built into Windows, encrypted under the user's Windows login (DPAPI). The `keyring` package Well RESTed already uses has a backend for it (`keyring.backends.Windows.WinVaultKeyring`). Supporting Windows means adding a second secret store next to the macOS one and choosing between them by platform. Everything else (SQLite, the cache, the admin UI) is cross-platform Python. The work is written up in [`docs/todo.md`](docs/todo.md#windows-support). On Linux the equivalent is the Secret Service (GNOME Keyring or KWallet).

**Don't work around it.** None of these is a substitute, because each one leaves tokens readable outside an OS-protected store:

- a plaintext `keyring` backend (such as `keyrings.alt`);
- tokens in environment variables or a `.env` file;
- running under WSL2. That's Linux, so it can't reach Credential Manager either.

Until Windows support lands, Windows users should run Well RESTed on a Mac.

## Quick start

This takes you from a fresh clone to a cached request in about ten minutes. Each step links to the fuller reference under [Usage](#usage).

**1. Check the prerequisites.** You need:
- a Mac (see [Platform support](#platform-support));
- `git`;
- Python 3.11 or newer. `python3 --version` tells you which you have. The installer from [python.org](https://www.python.org/downloads/macos/) is the simplest. Homebrew's Python works too, with the note in step 2.

**2. Clone and install.** Well RESTed runs from the repo with plain `python3`. It has no virtualenv and nothing to build.

```sh
git clone <this repo's URL> well-rested
cd well-rested
python3 -m pip install --user fastapi uvicorn 'httpx[socks]' keyring jinja2 python-multipart
python3 -m pip install --user pytest pytest-asyncio   # only to run the tests
```

> **Homebrew Python** refuses with `error: externally-managed-environment`. Add `--break-system-packages` to both `pip` lines. With `--user`, packages go only into your own user site, not Homebrew's. Or use python.org's Python instead.

**3. Start the proxy** with your first upstream. Its id (`jira` here) is the name clients put in `X-WR-Upstream`:

```sh
PROXY_UPSTREAMS=jira=https://your-site.atlassian.net python3 -m proxy
```

Leave it running, and open **http://localhost:1337/_admin** in a browser. `PROXY_UPSTREAMS` only matters on the first run. After that, add and edit upstreams on the Upstreams page.

**4. Add an identity**, in a second terminal (or on the Identities page). This is an Atlassian API token for Jira Cloud:

```sh
python3 -m proxy identity add me --scheme basic --username you@example.com --expires 30d \
	--rotate-url https://id.atlassian.com/manage-profile/security/api-tokens
```

Paste the token at the hidden prompt. The first time Well RESTed writes to the Keychain, macOS asks for permission. Choose **Always Allow**. You'll be asked once more after a Python upgrade. See [Set up an upstream, a rule and an identity](#set-up-an-upstream-a-rule-and-an-identity) for the bearer and custom schemes.

**5. Bind the identity and add a rule.**
- On the **Upstreams** page, tick `me` on the `jira` row.
- On the **Rules** page, add a rule for `jira`: pattern `/rest/**`, TTL 1 hour.

Nothing is cached without a rule.

**6. Make the same request twice:**

```sh
curl -s -o /dev/null -D - -H 'X-WR-Identity: me' http://localhost:1337/rest/api/2/myself | grep -i '^x-cache'
```

The first request says `X-Cache: MISS`. The second says `X-Cache: HIT`.
- **`PASS`** means no rule matched. Enter the path on the **Test a URL** page to see why.
- **`401`** means the upstream rejected the token.
- A **`400` or `403`** body says what's wrong (see [the status codes](#make-requests)).

**7. Point your projects at it.** A project sends requests to `http://localhost:1337` with the upstream's own path. It names its identity in `X-WR-Identity`, and never sends an `Authorization` header or holds a token. See [Make requests](#make-requests).

**To stop,** press Ctrl-C. **To start over,** stop the proxy and delete `proxy.db*` and `_cache/` (the runtime state, both gitignored). Tokens stay in the Keychain until you run `python3 -m proxy identity delete <name>`, so delete identities first if you want those gone too.

## Usage

### Install and run

Use plain `python3` (3.11 or newer) from the repo root, with no virtualenv. The dependencies are the ones in `pyproject.toml`. `httpx[socks]` adds SOCKS support for outbound proxies.

```sh
python3 -m pip install --user fastapi uvicorn 'httpx[socks]' keyring jinja2 python-multipart
python3 -m pip install --user pytest pytest-asyncio   # only to run the tests

PROXY_UPSTREAMS=jira=https://jira.example.com python3 -m proxy
```

On Homebrew's Python, add `--break-system-packages` to the `pip` lines (see the [Quick start](#quick-start)).

`PROXY_UPSTREAMS` only seeds the database on the first run. After that, manage upstreams from `/_admin/upstreams`. Runtime state is kept in `proxy.db` and `_cache/`, both gitignored.

Run the proxy in your GUI login session; the approval dialog cannot appear under a headless launchd agent. Always run a single worker. `python3 -m proxy` already does this.

### Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROXY_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` to serve LAN clients too. |
| `PROXY_PORT` | `1337` | Listen port. |
| `PROXY_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | `Host` header values the proxy answers. Any other `Host` gets a 400. |
| `PROXY_UPSTREAMS` | — | First-run seed, e.g. `jira=https://jira.example.com,wiki=https://wiki.example.com`. |
| `PROXY_DB_PATH` | `proxy.db` | SQLite file holding upstreams, rules, identities and approvals. |
| `PROXY_CACHE_DIR` | `_cache` | Directory for the shelf files, one per upstream. |
| `PROXY_CACHE_MAX_BYTES` | `524288000` (500 MB) | Per-upstream cache budget. Least recently used entries are evicted beyond it. |
| `PROXY_MAX_RESPONSE_BYTES` | `10485760` (10 MB) | Larger responses pass through uncached. |
| `PROXY_FLUSH_SECONDS` | `60` | How often in-memory hit counts are written to disk. |
| `PROXY_LOG_LEVEL` | `INFO` | `DEBUG` logs URLs. Don't use it with upstreams that accept secrets in the query string. |
| `PROXY_LOG_TAGS` | all | Comma-separated tag allowlist, e.g. `CACHE,PROXY`. |

Outbound proxy settings are not environment variables. The proxy ignores `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE` and `SSL_CERT_DIR` (upstream clients run with `trust_env=False`). Use the Proxies tab instead, with a per-proxy CA bundle where a proxy intercepts TLS. Trust is otherwise certifi's bundle.

### Set up an upstream, a rule and an identity

```sh
# a rule: cache everything under the jira upstream's /rest/ for an hour, keyed on the query string
curl -s -X POST http://localhost:1337/_admin/api/rules \
	-H 'Content-Type: application/json' \
	-d '{"upstream_id": "jira", "pattern": "/rest/**", "ttl_seconds": 3600, "query_mode": "include"}'

# an identity: reads the token with a hidden prompt (or pass --token-stdin)
python3 -m proxy identity add jira-uat --scheme basic --username you@example.com --expires 30d

# an upstream that wants e.g. "Authorization: Token abc123": a custom identity's token is the whole header value
#   python3 -m proxy identity add my-proj --scheme custom --expires 90d
# an upstream that wants another header, e.g. "x-api-key: …" (Claude's API) or "Cookie: session=…":
#   python3 -m proxy identity add claude --scheme custom --header x-api-key --expires 90d

# bind it to the jira upstream (or tick it on the Upstreams page); one identity can serve many upstreams
curl -s -X PUT http://localhost:1337/_admin/api/upstreams/jira \
	-H 'Content-Type: application/json' \
	-d '{"base_url": "https://jira.example.com", "verify_tls": true, "timeout_s": 30, "identities": ["jira-uat"]}'
```

An identity is a credential, so an account that reaches several sites is one identity bound to several upstreams. Its token is only ever sent to the upstreams it is bound to.

Every rule belongs to one upstream, and its pattern is the path as the client sends it (the upstream's own path, such as `/rest/api/2/issue/*`). Patterns use two wildcards: `*` matches within one path segment, and `**` matches across segments. When several of an upstream's rules match, the most specific one wins. The **Test a URL** page (`/_admin/test`) shows which rule wins and why.

For TTLs, `ttl_seconds: null` means the entry never expires, though it can still be evicted when the cache is full. `0` means bypass: never cache.

### Make requests

Send requests to `http://localhost:1337/<upstream path>`, the path exactly as the upstream expects it:

```sh
curl -si -H 'X-WR-Identity: jira-uat' -H 'X-WR-Upstream: jira' http://localhost:1337/rest/api/2/issue/ABC-123

# jira-uat is bound to the jira upstream only, so the upstream can be left out
curl -si -H 'X-WR-Identity: jira-uat' http://localhost:1337/rest/api/2/issue/ABC-123

# anonymous: no credentials are sent, and the upstream must always be named
curl -si -H 'X-WR-Upstream: jira' http://localhost:1337/rest/api/2/serverInfo
```

- **Upstream:** `X-WR-Upstream` holds an upstream id, in any letter case. Without it, the proxy uses the one upstream your identity is bound to; if it is bound to several, or you are anonymous, you get a `400` listing the choices. The header never travels upstream.
- **Redirects:** a `Location` that points at the upstream comes back pointing at the proxy (`http://localhost:1337/…`, using the `Host` you sent), so following it stays on the proxy. Send `X-WR-Upstream` on the follow-up too, unless it can be inferred.
- **Identity:** send `X-WR-Identity`, never `Authorization`. The proxy adds the upstream credentials itself. Omitting the header means anonymous access, with its own cache partition.
- **Cookies:** a client's `Cookie` header is stripped, and `Set-Cookie` never reaches the client.

**Response headers**

| Header | Meaning |
|--------|---------|
| `X-Cache: HIT` | Served from the cache. |
| `X-Cache: MISS` | Fetched from the upstream and stored. |
| `X-Cache: REFRESH` | The cached entry had expired; it was refetched and stored. |
| `X-Cache: STALE` | The upstream failed after retries, so the caller's own expired entry was served (also adds `Warning: 110`). |
| `X-Cache: PASS` | No caching applied. |
| `Age` | Seconds since the cached entry was fetched. |
| `Warning: 299 …` | The identity's token is expiring or was rejected. A human should rotate it. |
| `X-Cache-Invalidated: <n>` | Added to a successful `POST/PUT/PATCH/DELETE`: the number of the caller's cached entries purged for that path. |

**Status codes the proxy generates itself**

| Status | Meaning |
|--------|---------|
| `400` | Request bug: `Authorization` was sent; the identity is unknown or empty; `X-WR-Upstream` is unknown, empty or repeated (the body lists the configured upstreams); no `X-WR-Upstream` and the caller is anonymous or bound to several upstreams (the body lists them); a path segment is `.`/`..`; or the `Host` is not allowed. |
| `403` | The identity isn't bound to the upstream (or to any upstream), the LAN client was refused approval, or the request was for `/_admin` from off-machine. |
| `503` + `Retry-After` | Waiting for approval. Retry shortly. |
| `503` (no `Retry-After`) | No token in the Keychain for the identity, or the Keychain read failed. |
| `502` / `504` | The upstream failed or timed out with no stale entry to fall back on. Through an outbound proxy the failure is a `502` naming the proxy. |
| `503` (proxy) | The upstream's outbound proxy has no password in the Keychain (or the read failed), or its CA bundle is unusable. The body names the proxy only; the username and Keychain item are in the log. |

### Serving the LAN

```sh
PROXY_HOST=0.0.0.0 \
PROXY_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],<mac>.local,<lan-ip> \
python3 -m proxy
```

When a LAN client first uses an identity, a dialog appears on the Mac, and the same request also shows on `/_admin/approvals`. The first answer wins. The choices are:

- **Always:** approved until revoked.
- **One hour:** approved for an hour.
- **Session:** approved until the proxy restarts.
- **No:** refused, and prompting stops for a cooldown period.

`/_admin` itself always stays loopback-only.

### Admin UI

Open `http://localhost:1337/_admin`.

| Page | What it does |
|------|--------------|
| **Upstreams** | Add, edit and delete upstreams (each named by its upstream id), and tick which identities each one may use. Each row has a one-click copy of its `X-WR-Upstream: <id>` line. Deleting an upstream deletes its rules and unbinds its identities, leaving them otherwise untouched. |
| **Rules** | Create, edit, toggle and delete rules, each on one upstream, with live hit counts; filter the list by upstream. Rules can be copied from one upstream to another. |
| **Test a URL** | For an upstream (or none, to see what inference picks) plus a path and identity: the upstream the request would reach or the `400`/`403` it would get, which rule wins, the cache key, whether an entry exists, and the route (Direct or via a proxy). |
| **Cache** | Browse entries by upstream and partition. Purge one entry, by rule, by identity, or a whole upstream. |
| **Proxies** | Named outbound proxies: add, edit and delete, with the password written to the Keychain and never shown. An upstream chooses one on the Upstreams page, which also shows each upstream's route. |
| **Identities** | Health bars, bound upstreams, add, rotate and delete; the add form shows only the fields the chosen scheme uses. Each row has a one-click copy of its `X-WR-Identity: <name>` line. Tokens can be written but are never shown. Create identities here first, then bind them on Upstreams. |
| **Approvals** | Answer pending prompts; revoke Always and timed grants; clear No cooldowns. |

Every page has a JSON twin under `/_admin/api/…`, for example `GET /_admin/api/rules`.

### CLI

```sh
python3 -m proxy identity add <name> --scheme basic|bearer|custom --expires 30d \
	[--username <user>] [--header <name>] [--issued <when>] [--rotate-url <url>] [--token-stdin]
python3 -m proxy identity rotate <name> --expires 30d [--rotate-url <url>] [--token-stdin]
python3 -m proxy identity list
python3 -m proxy identity delete <name> [--yes]
python3 -m proxy approval list
python3 -m proxy approval revoke <address> <name>
```

### Tests

```sh
python3 -m pytest -q                                          # everything, ~8 s
python3 -m pytest tests/test_caching.py::test_miss_then_hit -q   # one test
```

## Status

What's built, what still needs checking by hand, and the known gaps.

### Done

| Plan | Delivered |
|------|-----------|
| **1 — Core caching proxy** | <ul><li>Routing by first path segment (`/jira/…` → the `jira` upstream); since replaced by header routing, below.</li><li>Wildcard rules (`*`, `**`) where the most specific match wins.</li><li>TTLs, including *never expires* and *bypass*.</li><li>Query modes `include` / `ignore` / `reject`.</li><li>One `shelve` cache per upstream.</li><li>Header handling: hop-by-hop headers, `Set-Cookie` and content encoding are cleaned up.</li></ul> |
| **1.5 — Explicit identities** | <ul><li>`X-WR-Identity` routing; tokens live only in the Keychain.</li><li>Token health shown as a battery, plus a `Warning: 299` header as expiry nears.</li><li>Host allowlist, and loopback-only `/_…` paths.</li><li>LAN client approvals through a native macOS dialog (Always / One hour / Session / No).</li><li>The `identity` / `approval` CLI.</li><li>Versioned SQLite schema with migrations.</li></ul> |
| **Shared-rule removal** | Every cache entry belongs to exactly one partition (an identity, or `anonymous`). Nothing is served across partitions. |
| **2 — Resilience** | <ul><li>GET retries with jittered backoff.</li><li>Single-flight: concurrent misses share one upstream fetch.</li><li>Stale-if-error.</li><li>Exact-path invalidation after successful writes.</li><li>Hit counting in memory, with no shelf write on a hit.</li><li>Per-upstream LRU eviction.</li><li>A startup sweep of entries belonging to deleted identities.</li></ul> |
| **3 — Admin UI** | `/_admin` pages for upstreams, rules (with hit counts, copy between upstreams, enable toggle), test-a-URL, cache browser and purges, identities (health bars, write-only tokens) and approvals (answer pending prompts, revoke grants). Each page has a JSON mirror under `/_admin/api/`. |
| **Identity bindings** | One identity (one account and token) can be bound to many upstreams, for example one Atlassian account across several sites. Create the identity first, then tick it on each upstream. Schema v4. The admin UI also has a favicon now, served at `/favicon.ico` too, so browsers stop logging 502s. |
| **Outbound proxies** | <ul><li>Named egress proxies (http, https, socks5, socks5h; SOCKS URLs need a port), managed on the Proxies page; an upstream routes through at most one, or goes Direct.</li><li>Proxy passwords live only in the Keychain.</li><li>An optional CA bundle per proxy, for TLS-intercepting proxies.</li><li>Proxy failures come back as a `502`/`503` naming the proxy, are served stale when possible, and never fall back to Direct.</li><li>Test a URL shows the route. Schema v5. The default port is now 1337.</li></ul> |
| **Custom identity scheme** | A third scheme, `custom`, for upstreams that want something other than Basic or Bearer, such as `Authorization: Token abc123`. The token is the whole header value and is sent exactly as stored. Schema v6. |
| **Custom header names** | A custom identity names the header it injects under: `--header x-api-key` for Claude's API, `--header Cookie` for a site that wants a session cookie, or nothing for `Authorization` as before. The name shows on the Identities page and in `identity list`; the value stays in the Keychain. A client header of the same name is replaced, never joined. Schema v9. |
| **Header routing** | <ul><li>The base URL is plain `http://localhost:1337`, and the path is forwarded untouched. There is no path prefix any more, and no fallback to one.</li><li>`X-WR-Upstream` names the upstream. Without it, an identity bound to exactly one upstream uses that one.</li><li>An unknown or ambiguous upstream gets a `400` listing the choices. An upstream the identity isn't bound to gets a `403`.</li><li>Rules belong to one upstream, and their patterns are the upstream's own paths. Upstreams are named by an *upstream id* (formerly "prefix") everywhere, JSON included. Schema v7 and v8.</li><li>A `Location` pointing at the upstream is rewritten to the proxy. Bodies are never touched.</li><li>Cache entries from before the change are cleared at the first start.</li></ul> |

`python3 -m pytest -q` → **750 passed, 2 skipped**. The skipped tests are the opt-in real-Keychain tests.

### Needs testing (by hand)

These steps need the real Keychain, the on-screen dialog, or a second machine on the LAN, so the automated suite can't cover them.

- [ ] **Keychain test:** `WR_TEST_KEYCHAIN=1 python3 -m pytest -q -m keychain`
- [ ] **Add an identity from the CLI.** The first Keychain write may ask for permission:
  `python3 -m proxy identity add jira-uat --scheme basic --username <email> --expires 30d --rotate-url https://id.atlassian.com/manage-profile/security/api-tokens`
  Then tick it on each upstream it should reach, on the Upstreams page.
- [ ] **One identity, two sites:** bind one identity to two upstreams and request both from loopback, then from a LAN machine; the LAN machine should be asked once, not once per site.
- [ ] **Add and rotate an identity in the UI.** Do both from `http://localhost:1337/_admin/identities`, and check that the health bar updates.
- [ ] **Request from loopback:** `curl -si -H 'X-WR-Identity: jira-uat' -H 'X-WR-Upstream: jira' http://localhost:1337/rest/api/2/myself` → `200`, with no prompt.
- [ ] **Inference:** with `jira-uat` bound to one upstream only, drop `X-WR-Upstream` from the same request → `200`. Bind it to a second upstream → `400` listing both.
- [ ] **Anonymous:** `curl -si -H 'X-WR-Upstream: jira' http://localhost:1337/rest/api/2/serverInfo` → `200` with no credentials sent; without the header → `400` listing the upstreams.
- [ ] **A real redirect:** request a path the upstream redirects (e.g. `curl -si -H 'X-WR-Upstream: jira' http://localhost:1337/secure/Dashboard.jspa` on Jira Server/DC). The `Location` should start with `http://localhost:1337/` when it points at the upstream, and be untouched when it points at an SSO host.
- [ ] **Old cache cleared:** on the first start after upgrading, the log says `cache layout changed; shelves cleared`, and the Cache page shows no `/jira/…` urls.
- [ ] **Request from a LAN machine.** Start the proxy bound to the LAN (see *Serving the LAN*), then make the same request from another machine. The dialog should appear on screen **and** on the Approvals page:
  - answer on the page → the dialog closes, and the client's retry succeeds;
  - answer the dialog instead → the page's pending row disappears within about 2 s.
- [ ] **Admin from the LAN:** `/_admin` requested from a LAN machine → `403`.
- [ ] **Custom scheme, real upstream.** Add the identity with `python3 -m proxy identity add <name> --scheme custom --expires 90d`, typing the whole value (e.g. `Token abc123`) at the prompt. Bind it on the Upstreams page, request through it → `200`.
- [ ] **Custom header name, real upstream.** `python3 -m proxy identity add claude --scheme custom --header x-api-key --expires 90d`, paste the API key at the `x-api-key value for 'claude':` prompt, bind it to an `https://api.anthropic.com` upstream, then `curl -si -H 'X-WR-Identity: claude' -H 'X-WR-Upstream: anthropic' -H 'anthropic-version: 2023-06-01' http://localhost:1337/v1/models` → `200`. Same with `--header Cookie` against a cookie-authenticated site. An existing custom identity from before the upgrade still sends `Authorization` (`identity list` shows it).
- [ ] **Outbound proxy, real egress.** `python3 -m proxy` listens on 1337, so open `http://localhost:1337/_admin/proxies` and add a real egress proxy with a password. Check that `security find-generic-password -s well-rested-proxy -a <name>` finds the item.
- [ ] **Route through it.** Set an upstream's proxy to it on the Upstreams page, then `curl -si -H 'X-WR-Identity: …' -H 'X-WR-Upstream: <upstream id>' http://localhost:1337/…` → `200`.
- [ ] **Wrong password:** enter one on the Proxies page → `502` saying the proxy "rejected credentials" or "refused the tunnel".
- [ ] **Missing Keychain item:** delete the item, then make any proxy edit → `503` naming the proxy.
- [ ] **TLS-intercepting proxy** (if one is available): set its CA bundle path and check that requests succeed.
- [ ] **SOCKS:** `ssh -D 1080 somehost`, then a proxy of `socks5h://127.0.0.1:1080` (SOCKS URLs need a port).
- [ ] **Battery display:** `python3 -m proxy identity list` shows each token's battery.

### Known gaps

- **Redirects across hosts:** only a `Location` pointing at the upstream itself is rewritten. A redirect to another host (an SSO login, a CDN) is passed through unchanged, and following it leaves the proxy. URLs inside response bodies (Jira's `self` links, for example) are never rewritten.
- **Unreachable upstream paths:** paths beginning with `/_` and `/favicon.ico` belong to the proxy, so an upstream's own `/_…` paths and favicon can't be reached through it.
- **Memory:** request and response bodies that pass through uncached are buffered in memory.
- **Invalidation scope:** it is exact-path only. A `POST` to a collection does not purge list or search GETs; those expire by TTL.
- **Private CAs on Direct upstreams:** `SSL_CERT_FILE` / `SSL_CERT_DIR` are ignored, and only proxies have a CA bundle so far. A per-upstream bundle is planned; see [`docs/todo.md`](docs/todo.md).
- **Approvals page lag:** if the on-screen dialog answers first, the pending row stays on the Approvals page until its next 2-second poll.
- **Deleted upstreams:** deleting an upstream leaves its shelf file on disk. Use *Purge all* on the cache page first.
- **Large shelves:** hit counts, the cache browser and purges each scan a whole shelf. That's fine at this scale, but would be slow with tens of thousands of entries.

## License

Mozilla Public License 2.0 — see [`LICENSE`](LICENSE). Each source file carries the MPL header.

Written by ran mak <ranvel@ranvel.com>.
