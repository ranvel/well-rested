# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""FastAPI app factory: wires settings, database, rule cache, identities, approvals, cache store, outbound proxies, and upstream client."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from proxy import db
from proxy.admin_router import router as admin_router
from proxy.admission import LOOPBACK_NAMES, HostGuard, is_loopback, parse_allowed_hosts
from proxy.approvals import ApprovalGate, OsascriptPrompter, Prompter
from proxy.cache import CacheStore
from proxy.config import Settings, parse_upstreams_seed
from proxy.flight import SingleFlight
from proxy.identities import IdentityRegistry
from proxy.keychain import PROXY_SERVICE, KeychainStore, SecretStore
from proxy.log import configure, get_logger
from proxy.outbound import OutboundProxies
from proxy.proxy_router import router as proxy_router
from proxy.upstream import UpstreamClient

log = get_logger(__name__, "MAIN")


def create_app(
	settings: Settings | None = None,
	transport: httpx.AsyncBaseTransport | None = None,
	secrets: SecretStore | None = None,
	prompter: Prompter | None = None,
	proxy_secrets: SecretStore | None = None,
) -> FastAPI:
	"""Build the proxy app. transport, secrets, proxy_secrets and prompter replace the network, the
	Keychain (identity tokens / outbound proxy passwords) and the macOS dialog (used by tests)."""
	settings = settings or Settings.from_env()
	configure(settings.log_level, settings.log_tags)
	allowed_hosts = parse_allowed_hosts(settings.allowed_hosts)   # a bad value fails at startup
	if settings.host not in LOOPBACK_NAMES and not is_loopback(settings.host) and allowed_hosts <= LOOPBACK_NAMES:
		log.warning(
			"bound beyond loopback but PROXY_ALLOWED_HOSTS lists only loopback names; "
			"LAN clients will get 400; host=%s", settings.host,
		)

	@asynccontextmanager
	async def lifespan(app: FastAPI):
		conn = db.connect(settings.db_path)
		db.seed_upstreams(conn, parse_upstreams_seed(settings.upstreams_seed))
		store = secrets if secrets is not None else KeychainStore()
		registry = IdentityRegistry(conn, store)
		await registry.refresh()   # the loading step: every identity's token, read from the Keychain once
		gate = ApprovalGate(
			conn,
			prompter if prompter is not None else OsascriptPrompter(),
			wait_s=settings.approval_wait_s,
			prompt_timeout_s=settings.approval_prompt_timeout_s,
			cooldown_s=settings.approval_cooldown_s,
		)
		app.state.settings = settings
		app.state.db = conn
		app.state.rules = db.RuleCache(conn)
		app.state.identities = registry
		app.state.secrets = store       # the admin UI writes tokens through the same store the registry reads
		app.state.approvals = gate
		app.state.cache = CacheStore(settings.cache_dir, max_bytes=settings.cache_max_bytes)
		app.state.flights = SingleFlight()
		app.state.upstream = UpstreamClient(transport=transport)
		proxy_store = proxy_secrets if proxy_secrets is not None else KeychainStore(PROXY_SERVICE)
		proxies = OutboundProxies(conn, proxy_store, on_reload=app.state.upstream.retire_proxied)
		await proxies.refresh()   # the loading step for outbound proxy passwords
		app.state.proxies = proxies
		app.state.proxy_secrets = proxy_store   # the admin UI writes proxy passwords through the same store
		live: dict[str, set[str]] = {}
		for identity in db.list_identities(conn):
			for upstream_id in identity.upstream_ids:
				live.setdefault(upstream_id, set()).add(identity.cache_identity)
		await app.state.cache.sweep_orphans(live)   # entries of deleted or unbound identities never show up again

		async def flush_loop():
			while True:
				await asyncio.sleep(settings.flush_seconds)
				try:
					await app.state.cache.flush_meta()
				except Exception as exc:
					log.warning("meta flush failed; error=%s", type(exc).__name__)

		flusher = asyncio.create_task(flush_loop())
		log.info("proxy started; db=%s cache_dir=%s", settings.db_path, settings.cache_dir)
		try:
			yield
		finally:
			await gate.aclose()
			await app.state.upstream.aclose()
			flusher.cancel()
			await asyncio.gather(flusher, return_exceptions=True)
			try:
				await app.state.cache.flush_meta()   # the final flush
			except Exception as exc:
				log.warning("final meta flush failed; error=%s", type(exc).__name__)
			app.state.cache.close()
			conn.close()
			log.info("proxy stopped")

	# No docs/openapi routes: every path except /_admin belongs to an upstream.
	app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
	# First match wins: the admin UI and the favicon must come before the proxy's catch-all.
	static = Path(__file__).parent / "static"
	app.mount("/_admin/static", StaticFiles(directory=str(static)), name="admin-static")
	app.include_router(admin_router, prefix="/_admin")

	@app.get("/favicon.ico", include_in_schema=False)
	async def favicon() -> FileResponse:
		# Browsers ask for it unprompted, and with no X-WR-Upstream it could only be a 400.
		return FileResponse(static / "favicon.png", media_type="image/png")

	app.include_router(proxy_router)
	app.add_middleware(HostGuard, allowed_hosts=allowed_hosts)
	return app
