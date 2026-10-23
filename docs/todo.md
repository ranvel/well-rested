# To-do

Work that's agreed but not scheduled. Each item says why it exists and what it should look like, so whoever picks it up can go straight to a plan.

## Per-upstream CA bundle

*Added 2026-10-14. Not started.*

### Why

Since the outbound-proxies work (design amendment 37), every upstream client runs with `trust_env=False`. That stops shell `HTTP(S)_PROXY` variables from rerouting traffic, but it also means `SSL_CERT_FILE` / `SSL_CERT_DIR` are no longer honoured. Trust is now certifi, plus a proxy's own CA bundle for upstreams routed through that proxy.

That leaves a gap for **Direct** upstreams behind a private CA, such as an internal Jira behind a private CA, reached over a VPN. The only workaround today is unticking Verify TLS on that upstream, which is worse than either alternative. Nothing relies on the environment variables right now, so nothing is broken yet.

### Decision: don't bring the environment variables back

- **They replace trust rather than extend it.** httpx swaps out certifi for the named file, so a bundle holding only one site's CA breaks every public upstream.
- **They depend on how the server was launched.** A terminal, launchd and an IDE each provide a different environment. This is the same trap `trust_env=False` closed for `HTTP_PROXY`.
- **They're global.** Usually one upstream needs one CA.

### Shape

- **Schema v10** (v7 and v8 went to header routing, v9 to custom header names): `ALTER TABLE upstreams ADD COLUMN ca_bundle TEXT`. NULL means certifi only. A plain `ADD COLUMN`, so no rebuild.
- **Trust:** an upstream is verified against certifi, plus its own `ca_bundle`, plus its proxy's `ca_bundle` when it's routed through one. Reuse `proxy.outbound.check_ca_bundle` (save-time validation: an absolute path to a loadable PEM) and `tls_context`, extending `tls_context` to take more than one bundle.
- **Client pool:** `UpstreamClient` keys clients by `(verify_tls, proxy name, generation)`. Two Direct upstreams with different bundles would share a client, so the upstream's bundle path must join the key. This needs a test showing two upstreams with different bundles never share trust. Changing an upstream's bundle picks a new client on its next request; the old one can be retired the way proxy clients are.
- **Errors:** a bundle that has gone missing or unreadable since save gives a `503` naming the upstream, before any network call. That's the same pattern as an unusable proxy bundle, and it never falls back to an unverified connection.
- **UI:** a CA bundle text field on the Upstreams row and add form, matching the Proxies tab. JSON gets `ca_bundle`, and leaving it out on an edit keeps the current value, like `proxy`. Test a URL could show it next to the route.
- **Docs:** a design amendment, README (configuration and smoke step), project index, and a handoff.

### Size

About the size of the custom identity scheme change: one migration, a few validation and pool changes, and one admin column.

## Windows support

*Added 2026-10-22. Not started.*

### Why

Windows users can't run Well RESTed. `KeychainStore` only uses `keyring`'s macOS backend and raises when it isn't viable, so on Windows the proxy and the CLI both stop at startup. That's the right failure: tokens must live only in an OS-protected store, and Windows has one. Nothing uses it yet.

### Decision: Windows Credential Manager, nothing weaker

- **Credential Manager** is the per-user credential vault built into Windows, encrypted under the user's login (DPAPI). It plays the Keychain's role.
- `keyring` already has a backend for it, `keyring.backends.Windows.WinVaultKeyring` (it pulls in `pywin32-ctypes` on Windows).
- **No stand-ins:** no plaintext `keyrings.alt` backend, no environment variables or `.env` files, and no WSL2 (that's Linux, and can't reach Credential Manager). Each would put tokens somewhere other processes or files can read them.

### Shape

- **Secret store:** a `WindowsCredentialStore` in `proxy/keychain.py` that implements `SecretStore` on `WinVaultKeyring` directly. Like `KeychainStore`, it never consults `keyring.get_keyring()`, and wraps backend errors in `KeychainError` so the 503 paths keep working. It uses the same two services, `well-rested` and `well-rested-proxy`.
- **Selection:** a small factory (say `default_store(service)`) that picks by `sys.platform`, used by `main.py` (both stores) and `cli.py` instead of naming `KeychainStore`. Any other platform keeps today's refusal.
- **Things to check against the real backend:**
	- one service holding many accounts (`WinVaultKeyring` names credentials by service and user, and has a compatibility scheme when several users share a service);
	- the credential blob size limit (2,560 bytes), which a long `Cookie` value could exceed. Refuse oversized tokens at add/rotate time with a clear message.
- **Approval dialog:** the `Prompter` protocol is already pluggable. Either add a Windows prompter (a PowerShell message box, run as argv the way `OsascriptPrompter` is), or ship Approvals-page-only on Windows and say so. Check that `ApprovalGate` behaves when no on-screen prompter exists.
- **Tests:**
	- an opt-in real Credential Manager test mirroring the Keychain one;
	- `skipif` markers on the darwin-only tests (`osascript` fakes, the Keychain test);
	- the full suite run on Windows once.
	- Shelve uses `dbm.sqlite3` on Python 3.13+ on every platform, so the cache should need nothing.
- **Docs:**
	- README platform table and quick start (Windows install, `python` vs `python3`);
	- the Credential Manager equivalent of the `security find-generic-password` checks (`cmdkey /list`, or the Credential Manager control panel);
	- a design amendment, project index, and a handoff.

### Size

Small in code: one store class, a factory and two call sites. Most of the effort is checking it by hand on a real Windows machine, which needs a Windows machine.
