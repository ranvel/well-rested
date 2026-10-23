# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin pages and JSON API under /_admin.

Loopback-only: HostGuard refuses non-loopback clients before routing. Every
endpoint computes one payload dict; /_admin/api/... returns it as JSON and the
page route renders it — one set of logic, two faces. Handlers are async def and
share the event loop (and the SQLite connection) with the proxy.

Page forms follow POST → redirect → GET, with the outcome carried as ?m= and
shown in the flash. A refused form re-renders the page with the error and a 4xx.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from proxy import db
from proxy.approvals import Decision
from proxy.cache import ANONYMOUS, age_seconds, build_key, is_fresh, normalize_query
from proxy.identities import add_identity, health, parse_when, remove_identity, rotate_token, stored_identity
from proxy.log import get_logger
from proxy.outbound import add_proxy, edit_proxy, remove_proxy
from proxy.proxy_router import RoutingRefusal, choose_upstream
from proxy.rules import specificity

log = get_logger(__name__, "ADMIN")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_REFUSALS = (ValueError, LookupError, sqlite3.IntegrityError)   # bad input, unknown row, duplicate
_IDENTITY_FAILURES = (*_REFUSALS, RuntimeError)                # plus a Keychain that refuses (KeychainError)
_PROXY_FAILURES = (*_REFUSALS, RuntimeError)   # plus a Keychain that refuses (KeychainError)


def page(request: Request, name: str, payload: dict, status: int = 200) -> Response:
	context = {"message": request.query_params.get("m"), **payload}
	response = templates.TemplateResponse(request, name, context, status_code=status)
	response.headers["Cache-Control"] = "no-store"   # the UI shows live state; a browser cache would lie
	return response


def api(payload: dict, status: int = 200) -> JSONResponse:
	response = JSONResponse(payload, status_code=status)
	response.headers["Cache-Control"] = "no-store"
	return response


def redirect(path: str, message: str) -> Response:
	return RedirectResponse(f"{path}?{urlencode({'m': message})}", status_code=303)


def _status_for(exc: Exception) -> int:
	if isinstance(exc, LookupError):
		return 404
	if isinstance(exc, RuntimeError):
		return 500   # the Keychain refused; nothing the caller typed was wrong
	return 400


def refused(request: Request, name: str, payload: dict, exc: Exception) -> Response:
	"""The page face of a refused write: the same page, the error in the flash, a 4xx."""
	log.debug("refused; path=%s error=%s", request.url.path, exc)
	return page(request, name, {**payload, "message": str(exc), "error": True}, status=_status_for(exc))


def api_refused(exc: Exception) -> JSONResponse:
	return api({"error": str(exc)}, status=_status_for(exc))


async def form(request: Request) -> dict:
	"""The submitted fields, from a JSON body (API) or a form (page), as a plain dict.
	A form field sent more than once (checkboxes sharing a name) becomes a list."""
	if request.headers.get("content-type", "").startswith("application/json"):
		body = await request.json()
		if not isinstance(body, dict):
			raise ValueError("the JSON body must be an object")
		return body
	data: dict = {}
	for k, v in (await request.form()).multi_items():
		if k not in data:
			data[k] = v
		elif isinstance(data[k], list):
			data[k].append(v)
		else:
			data[k] = [data[k], v]
	return data


def _checkbox(data: dict, name: str) -> bool:
	value = data.get(name, False)
	if isinstance(value, bool):
		return value
	return str(value).lower() in ("on", "true", "1", "yes")


def _text(data: dict, name: str, required: bool = True) -> str:
	value = str(data.get(name, "") or "").strip()
	if required and not value:
		raise ValueError(f"{name} is required")
	return value


@router.get("")
@router.get("/")
async def index(request: Request) -> Response:
	return RedirectResponse("/_admin/rules", status_code=303)


# --- upstreams -----------------------------------------------------------------

def _upstream_dict(upstream: db.Upstream, bound: dict[str, list[str]]) -> dict:
	identities = bound.get(upstream.upstream_id, [])
	return {
		"upstream_id": upstream.upstream_id,
		"base_url": upstream.base_url,
		"verify_tls": upstream.verify_tls,
		"timeout_s": upstream.timeout_s,
		"identities": identities,
		"proxy": upstream.proxy,
		# Over http:// the outbound proxy sees the injected credential header; https:// keeps it in the tunnel.
		"plaintext_via_proxy": upstream.proxy is not None and upstream.base_url.startswith("http://") and bool(identities),
	}


async def _upstreams_payload(request: Request) -> dict:
	conn = request.app.state.db
	bound = db.identities_by_upstream(conn)
	return {
		"upstreams": [_upstream_dict(u, bound) for u in db.list_upstreams(conn)],
		"identity_names": [i.name for i in db.list_identities(conn)],
		"proxy_names": [p.name for p in db.list_proxies(conn)],
	}


def _timeout(data: dict) -> float:
	try:
		timeout_s = float(_text(data, "timeout_s"))
	except ValueError:
		raise ValueError(f"timeout must be a number of seconds: {data.get('timeout_s')!r}") from None
	if not 0.1 <= timeout_s <= 600:
		raise ValueError(f"timeout must be between 0.1 and 600 seconds: {timeout_s}")
	return timeout_s


def _identity_names(data: dict) -> list[str] | None:
	"""The identities to bind: a JSON list, or the page's checkboxes (identities_sent marks a form
	that carried them, so all-unchecked unbinds everyone). None leaves the bindings unchanged."""
	if "identities" not in data and not data.get("identities_sent"):
		return None
	names = data.get("identities", [])
	if isinstance(names, str) and "identities_sent" in data:
		names = [names]   # a single checked box
	if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
		raise ValueError("identities must be a list of identity names")
	return names


def _proxy_choice(data: dict, current: db.Upstream | None) -> str | None:
	"""The outbound proxy to route through: '' (the page's Direct) or null means Direct. Omitted on
	an edit means unchanged, so an older API client doesn't silently drop a route."""
	if "proxy" not in data:
		return current.proxy if current else None
	value = data["proxy"]
	if value is not None and not isinstance(value, str):
		raise ValueError("proxy must be a proxy name, or empty for Direct")
	return (value or "").strip() or None


def _save_upstream(request: Request, data: dict, upstream_id: str | None = None) -> db.Upstream:
	"""Create or edit (the upstream id is immutable on edit), with its identity bindings in the same write."""
	upstream_id = upstream_id or _text(data, "upstream_id")
	current = db.get_upstream(request.app.state.db, upstream_id)
	return db.put_upstream(request.app.state.db, db.Upstream(
		upstream_id=upstream_id,
		base_url=_text(data, "base_url"),
		verify_tls=_checkbox(data, "verify_tls"),
		timeout_s=_timeout(data),
		proxy=_proxy_choice(data, current),
	), identities=_identity_names(data))


@router.get("/upstreams")
async def upstreams_page(request: Request) -> Response:
	return page(request, "upstreams.html", await _upstreams_payload(request))


@router.get("/api/upstreams")
async def upstreams_api(request: Request) -> Response:
	return api({"upstreams": (await _upstreams_payload(request))["upstreams"]})


@router.post("/upstreams")
async def upstream_save_page(request: Request) -> Response:
	try:
		upstream = _save_upstream(request, await form(request))
	except _REFUSALS as exc:
		return refused(request, "upstreams.html", await _upstreams_payload(request), exc)
	return redirect("/_admin/upstreams", f"saved upstream '{upstream.upstream_id}'")


@router.post("/api/upstreams")
async def upstream_create_api(request: Request) -> Response:
	try:
		upstream = _save_upstream(request, await form(request))
	except _REFUSALS as exc:
		return api_refused(exc)
	return api(_upstream_dict(upstream, db.identities_by_upstream(request.app.state.db)), status=201)


@router.put("/api/upstreams/{upstream_id}")
async def upstream_edit_api(request: Request, upstream_id: str) -> Response:
	conn = request.app.state.db
	try:
		if db.get_upstream(conn, upstream_id) is None:
			raise LookupError(f"no upstream {upstream_id!r}")
		upstream = _save_upstream(request, await form(request), upstream_id=upstream_id)
	except _REFUSALS as exc:
		return api_refused(exc)
	return api(_upstream_dict(upstream, db.identities_by_upstream(conn)))


@router.post("/upstreams/{upstream_id}/delete")
async def upstream_delete_page(request: Request, upstream_id: str) -> Response:
	try:
		if not db.delete_upstream(request.app.state.db, upstream_id):
			raise LookupError(f"no upstream {upstream_id!r}")
	except _REFUSALS as exc:
		return refused(request, "upstreams.html", await _upstreams_payload(request), exc)
	return redirect("/_admin/upstreams", f"deleted upstream '{upstream_id}'")


@router.delete("/api/upstreams/{upstream_id}")
async def upstream_delete_api(request: Request, upstream_id: str) -> Response:
	try:
		if not db.delete_upstream(request.app.state.db, upstream_id):
			raise LookupError(f"no upstream {upstream_id!r}")
	except _REFUSALS as exc:
		return api_refused(exc)
	return api({"deleted": True, "upstream_id": upstream_id})


# --- rules -------------------------------------------------------------------

_TTL_RE = re.compile(r"(\d+)\s*([smhd])")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
TTL_PRESETS = [("1m", 60), ("2m", 120), ("10m", 600), ("1h", 3600), ("9h", 32400), ("1d", 86400)]
QUERY_MODE_HELP = [
	("include", "the query string is part of the cache key (sorted, so ?a=1&b=2 and ?b=2&a=1 share an entry)"),
	("ignore", "the query string is dropped from the key: every variant shares one entry"),
	("reject", "a request with a query string passes through uncached"),
]


def parse_ttl(preset: str, custom: str = "") -> int | None:
	"""Form input → ttl_seconds. 'never' → None, 'bypass' → 0, presets by value, 'custom' parses 90s/45m/3h/2d."""
	if preset == "never":
		return None
	if preset == "bypass":
		return 0
	if preset != "custom":
		try:
			return int(preset)
		except ValueError:
			raise ValueError(f"TTL must be a preset, never, bypass or custom: {preset!r}") from None
	match = _TTL_RE.fullmatch(custom.strip().lower())
	if not match:
		raise ValueError(f"custom TTL must look like 90s, 45m, 3h or 2d: {custom!r}")
	return int(match.group(1)) * _UNIT_S[match.group(2)]


def ttl_human(ttl: int | None) -> str:
	if ttl is None:
		return "never expires"
	if ttl == 0:
		return "bypass"
	for unit, seconds in (("d", 86400), ("h", 3600), ("m", 60)):
		if ttl % seconds == 0 and ttl >= seconds:
			return f"{ttl // seconds}{unit}"
	return f"{ttl}s"


templates.env.filters["ttl_human"] = ttl_human


def _rule_dict(rule) -> dict:
	return {
		"id": rule.id,
		"upstream_id": rule.upstream_id,
		"pattern": rule.pattern,
		"ttl_seconds": rule.ttl_seconds,
		"ttl": ttl_human(rule.ttl_seconds),
		"query_mode": rule.query_mode,
		"enabled": rule.enabled,
		"note": rule.note,
	}


def _rule_fields(data: dict, existing=None) -> dict:
	"""Submitted rule fields → update_rule/create_rule keywords. The API may send ttl_seconds
	directly; the form sends a ttl choice plus ttl_custom."""
	if "ttl_seconds" in data:
		ttl_seconds = data["ttl_seconds"]
		if ttl_seconds is not None and not isinstance(ttl_seconds, int):
			raise ValueError(f"ttl_seconds must be an integer or null: {ttl_seconds!r}")
	else:
		ttl_seconds = parse_ttl(_text(data, "ttl"), str(data.get("ttl_custom") or ""))
	if "enabled" in data:
		enabled = _checkbox(data, "enabled")
	else:
		enabled = existing.enabled if existing is not None else True
	note = data.get("note")
	note = str(note).strip() if note is not None else None
	upstream_id = _text(data, "upstream_id", required=existing is None) or existing.upstream_id
	return dict(
		upstream_id=upstream_id,
		pattern=_text(data, "pattern"),
		ttl_seconds=ttl_seconds,
		query_mode=_text(data, "query_mode", required=False) or "include",
		enabled=enabled,
		note=note or None,
	)


def _rule_form_payload(request: Request, rule=None, data: dict | None = None) -> dict:
	"""What rule_form.html needs: the rule (or blanks), the TTL choice and custom text, the presets, the upstreams."""
	if data is not None:   # a refused submission: show what was typed
		fields = {
			"id": rule.id if rule else None,
			"upstream_id": data.get("upstream_id", rule.upstream_id if rule else ""),
			"pattern": data.get("pattern", ""),
			"query_mode": data.get("query_mode", "include"),
			"enabled": _checkbox(data, "enabled") if "enabled" in data else (rule.enabled if rule else True),
			"note": data.get("note") or "",
		}
		ttl_choice, ttl_custom = data.get("ttl", "3600"), data.get("ttl_custom", "")
	elif rule is not None:
		fields = _rule_dict(rule)
		ttl = rule.ttl_seconds
		if ttl is None:
			ttl_choice, ttl_custom = "never", ""
		elif ttl == 0:
			ttl_choice, ttl_custom = "bypass", ""
		elif ttl in dict((s, l) for l, s in TTL_PRESETS):
			ttl_choice, ttl_custom = str(ttl), ""
		else:
			ttl_choice, ttl_custom = "custom", ttl_human(ttl)
	else:
		upstream_id = request.query_params.get("upstream", "")
		fields = {"id": None, "upstream_id": upstream_id, "pattern": "", "query_mode": "include", "enabled": True, "note": ""}
		ttl_choice, ttl_custom = "3600", ""
	return {
		"rule": fields,
		"upstreams": [u.upstream_id for u in db.list_upstreams(request.app.state.db)],
		"ttl_choice": ttl_choice,
		"ttl_custom": ttl_custom,
		"presets": TTL_PRESETS,
		"query_modes": QUERY_MODE_HELP,
		"action": f"/_admin/rules/{rule.id}" if rule is not None else "/_admin/rules",
	}


def _get_rule_or_404(request: Request, rule_id: int):
	rule = db.get_rule(request.app.state.db, rule_id)
	if rule is None:
		raise LookupError(f"no rule with id {rule_id}")
	return rule


async def _rules_payload(request: Request) -> dict:
	"""Every rule, or one upstream's (?upstream=), ordered by upstream."""
	conn = request.app.state.db
	upstream_id = request.query_params.get("upstream", "")
	rules = [_rule_dict(rule) for rule in db.list_rules(conn) if not upstream_id or rule.upstream_id == upstream_id]
	hits = await request.app.state.cache.hits_by_rule()
	return {"rules": rules, "hits": hits, "upstream": upstream_id, "upstreams": [u.upstream_id for u in db.list_upstreams(conn)]}


@router.get("/rules")
async def rules_page(request: Request) -> Response:
	return page(request, "rules.html", await _rules_payload(request))


@router.get("/api/rules")
async def rules_api(request: Request) -> Response:
	payload = await _rules_payload(request)
	return api({"rules": [{**rule, "hits": payload["hits"].get(rule["id"], 0)} for rule in payload["rules"]]})


@router.get("/rules/new")
async def rule_new_page(request: Request) -> Response:
	return page(request, "rule_form.html", _rule_form_payload(request))


# "copy" is registered before the /rules/{rule_id} routes so it is never read as an id.

def _copy_payload(request: Request, report: dict | None = None) -> dict:
	return {"upstreams": [u.upstream_id for u in db.list_upstreams(request.app.state.db)], "report": report}


def _copy(request: Request, data: dict) -> dict:
	source, target = _text(data, "source"), _text(data, "target")
	copied, skipped = db.copy_rules(request.app.state.db, source, target)
	return {"source": source, "target": target, "copied": copied, "skipped": skipped}


@router.get("/rules/copy")
async def rules_copy_page(request: Request) -> Response:
	return page(request, "copy_rules.html", _copy_payload(request))


@router.post("/rules/copy")
async def rules_copy_submit(request: Request) -> Response:
	try:
		report = _copy(request, await form(request))
	except _REFUSALS as exc:
		return refused(request, "copy_rules.html", _copy_payload(request), exc)
	return page(request, "copy_rules.html", _copy_payload(request, report))


@router.post("/api/rules/copy")
async def rules_copy_api(request: Request) -> Response:
	try:
		return api(_copy(request, await form(request)))
	except _REFUSALS as exc:
		return api_refused(exc)


@router.get("/rules/{rule_id}/edit")
async def rule_edit_page(request: Request, rule_id: int) -> Response:
	try:
		rule = _get_rule_or_404(request, rule_id)
	except LookupError as exc:
		return refused(request, "rules.html", await _rules_payload(request), exc)
	return page(request, "rule_form.html", _rule_form_payload(request, rule))


@router.post("/rules")
async def rule_create_page(request: Request) -> Response:
	data = await form(request)
	try:
		rule = db.create_rule(request.app.state.db, **_rule_fields(data))
	except _REFUSALS as exc:
		return refused(request, "rule_form.html", _rule_form_payload(request, data=data), exc)
	return redirect("/_admin/rules", f"created rule {rule.pattern}")


@router.post("/api/rules")
async def rule_create_api(request: Request) -> Response:
	try:
		rule = db.create_rule(request.app.state.db, **_rule_fields(await form(request)))
	except _REFUSALS as exc:
		return api_refused(exc)
	return api({**_rule_dict(rule), "hits": 0}, status=201)


@router.post("/rules/{rule_id}")
async def rule_update_page(request: Request, rule_id: int) -> Response:
	data = await form(request)
	try:
		rule = _get_rule_or_404(request, rule_id)
		rule = db.update_rule(request.app.state.db, rule_id, **_rule_fields(data, rule))
	except LookupError as exc:
		return refused(request, "rules.html", await _rules_payload(request), exc)
	except _REFUSALS as exc:
		return refused(request, "rule_form.html", _rule_form_payload(request, rule, data=data), exc)
	return redirect("/_admin/rules", f"saved rule {rule.pattern}")


@router.put("/api/rules/{rule_id}")
async def rule_update_api(request: Request, rule_id: int) -> Response:
	try:
		rule = _get_rule_or_404(request, rule_id)
		rule = db.update_rule(request.app.state.db, rule_id, **_rule_fields(await form(request), rule))
	except _REFUSALS as exc:
		return api_refused(exc)
	hits = await request.app.state.cache.hits_by_rule()
	return api({**_rule_dict(rule), "hits": hits.get(rule.id, 0)})


@router.post("/rules/{rule_id}/delete")
async def rule_delete_page(request: Request, rule_id: int) -> Response:
	try:
		if not db.delete_rule(request.app.state.db, rule_id):
			raise LookupError(f"no rule with id {rule_id}")
	except _REFUSALS as exc:
		return refused(request, "rules.html", await _rules_payload(request), exc)
	return redirect("/_admin/rules", "deleted the rule")


@router.delete("/api/rules/{rule_id}")
async def rule_delete_api(request: Request, rule_id: int) -> Response:
	try:
		if not db.delete_rule(request.app.state.db, rule_id):
			raise LookupError(f"no rule with id {rule_id}")
	except _REFUSALS as exc:
		return api_refused(exc)
	return api({"deleted": True, "id": rule_id})


# --- cache browser ---------------------------------------------------------------

def bytes_human(n: int) -> str:
	if n < 1024:
		return f"{n} B"
	for unit in ("KB", "MB", "GB", "TB"):
		n /= 1024
		if n < 1024 or unit == "TB":
			return f"{n:.1f} {unit}"


def age_human(seconds: int) -> str:
	for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
		if seconds >= size:
			return f"{seconds // size}{unit}"
	return f"{seconds}s"


def datetime_utc(epoch: int) -> str:
	return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["bytes_human"] = bytes_human
templates.env.filters["age_human"] = age_human
templates.env.filters["datetime_utc"] = datetime_utc


async def _cache_payload(request: Request, upstream_id: str | None, identity: str, message: str | None = None) -> dict:
	conn = request.app.state.db
	cache = request.app.state.cache
	upstream_ids = sorted({u.upstream_id for u in db.list_upstreams(conn)} | cache.known_upstream_ids())
	if not upstream_id:
		upstream_id = upstream_ids[0] if upstream_ids else ""
	entries = await cache.browse(upstream_id) if upstream_id else []
	patterns = {rule.id: rule.pattern for rule in db.list_rules(conn)}
	for row in entries:
		row["rule"] = patterns.get(row["rule_id"])   # None once the rule is deleted
	partitions = {row["identity"] for row in entries}
	identities = ["all", ANONYMOUS, *(i.cache_identity for i in db.list_identities(conn))]
	identities += sorted(partitions - set(identities))
	if identity and identity != "all":
		entries = [row for row in entries if row["identity"] == identity]
	return {
		"upstream_id": upstream_id,
		"upstream_ids": upstream_ids,
		"identity": identity or "all",
		"identities": identities,
		"entries": entries,
		"count": len(entries),
		"bytes": sum(row["bytes"] for row in entries),
		"purged": message,
	}


def _shelf_id(upstream_id: str | None) -> str | None:
	"""A submitted shelf name, refused unless it is an upstream id: it becomes part of a file path."""
	if upstream_id and not db.UPSTREAM_ID_RE.fullmatch(upstream_id):
		raise ValueError(f"not an upstream id: {upstream_id[:64]!r}")
	return upstream_id


@router.get("/cache")
async def cache_page(request: Request) -> Response:
	q = request.query_params
	try:
		upstream_id = _shelf_id(q.get("upstream_id"))
	except ValueError as exc:
		return refused(request, "cache.html", await _cache_payload(request, None, "all"), exc)
	return page(request, "cache.html", await _cache_payload(request, upstream_id, q.get("identity", "all")))


@router.get("/api/cache")
async def cache_api(request: Request) -> Response:
	q = request.query_params
	try:
		upstream_id = _shelf_id(q.get("upstream_id"))
	except ValueError as exc:
		return api_refused(exc)
	payload = await _cache_payload(request, upstream_id, q.get("identity", "all"))
	return api({k: v for k, v in payload.items() if k != "purged"})


async def _purge(request: Request, upstream_id: str, data: dict) -> int:
	"""Exactly one of key / rule_id / identity / all, as the page's purge buttons send them."""
	cache = request.app.state.cache
	targets = [name for name in ("key", "rule_id", "identity", "all") if data.get(name)]
	if len(targets) != 1:
		raise ValueError("purge takes exactly one of key, rule_id, identity or all")
	target = targets[0]
	if target == "key":
		return int(await cache.purge_entry(upstream_id, str(data["key"])))
	if target == "rule_id":
		try:
			rule_id = int(data["rule_id"])
		except (TypeError, ValueError):
			raise ValueError(f"rule_id must be an integer: {data['rule_id']!r}") from None
		return await cache.purge_by_rule(rule_id)
	if target == "identity":
		return await cache.purge_by_identity(str(data["identity"]))
	return await cache.purge_all(upstream_id)


@router.post("/cache/purge")
async def cache_purge_page(request: Request) -> Response:
	"""Answers with the entries partial, which HTMX swaps into #entries."""
	data = await form(request)
	upstream_id = _text(data, "upstream_id", required=False)
	identity = str(data.get("filter") or "all")
	try:
		_shelf_id(upstream_id)
	except ValueError as exc:
		return page(request, "cache_entries.html", await _cache_payload(request, None, identity, f"error: {exc}"), status=400)
	try:
		purged = await _purge(request, upstream_id, data)
	except _REFUSALS as exc:   # the partial again, with the error; base.html tells htmx to swap 4xx too
		return page(request, "cache_entries.html", await _cache_payload(request, upstream_id, identity, f"error: {exc}"), status=_status_for(exc))
	log.info("purged from the admin page; upstream=%s count=%d", upstream_id, purged)
	return page(request, "cache_entries.html", await _cache_payload(request, upstream_id, identity, f"purged {purged}"))


@router.delete("/api/cache/rules/{rule_id}")
async def cache_purge_rule_api(request: Request, rule_id: int) -> Response:
	return api({"purged": await request.app.state.cache.purge_by_rule(rule_id)})


@router.delete("/api/cache/identities/{identity}")
async def cache_purge_identity_api(request: Request, identity: str) -> Response:
	return api({"purged": await request.app.state.cache.purge_by_identity(identity)})


@router.delete("/api/cache/{upstream_id}/entries/{key}")
async def cache_purge_entry_api(request: Request, upstream_id: str, key: str) -> Response:
	try:
		_shelf_id(upstream_id)
	except ValueError as exc:
		return api_refused(exc)
	if not await request.app.state.cache.purge_entry(upstream_id, key):
		return api_refused(LookupError(f"no entry {key} in shelf {upstream_id!r}"))
	return api({"purged": 1})


@router.delete("/api/cache/{upstream_id}")
async def cache_purge_all_api(request: Request, upstream_id: str) -> Response:
	try:
		_shelf_id(upstream_id)
	except ValueError as exc:
		return api_refused(exc)
	return api({"purged": await request.app.state.cache.purge_all(upstream_id)})


# --- identities -------------------------------------------------------------------
#
# A token is write-only here: the forms take one, no response ever carries one, and a
# refused form never echoes it back.

def _iso(value: datetime | None) -> str | None:
	return value.isoformat(timespec="seconds") if value else None


def _identity_dict(identity: db.Identity, registry, now: datetime) -> dict:
	h = health(identity, now)
	return {
		"name": identity.name,
		"upstreams": list(identity.upstream_ids),
		"scheme": identity.scheme,
		"username": identity.username,
		"header": identity.header,
		"issued_at": _iso(identity.issued_at),
		"expires_at": _iso(identity.expires_at),
		"rotate_url": identity.rotate_url,
		"last_status": identity.last_status,
		"last_status_at": _iso(identity.last_status_at),
		"health": {"fraction": h.fraction, "level": h.level},
		"has_token": registry.has_token(identity),
		"load_error": registry.load_error(identity),
	}


def _identity_form(data: dict | None = None) -> dict:
	"""The add form's fields as typed, never the token."""
	data = data or {}
	return {
		"name": data.get("name", ""), "scheme": data.get("scheme", "basic"),
		"username": data.get("username", ""), "header": data.get("header", ""),
		"issued": data.get("issued", ""), "expires": data.get("expires", "30d"),
		"rotate_url": data.get("rotate_url", ""),
	}


async def _identities_payload(request: Request, data: dict | None = None) -> dict:
	conn = request.app.state.db
	registry = request.app.state.identities
	await registry.refresh()
	now = datetime.now(timezone.utc)
	return {
		"identities": [_identity_dict(i, registry, now) for i in db.list_identities(conn)],
		"form": _identity_form(data),
	}


def _lifetime(data: dict, now: datetime) -> tuple[datetime, datetime]:
	issued = _text(data, "issued", required=False)
	return parse_when(issued, now) if issued else now, parse_when(_text(data, "expires"), now)


async def _add(request: Request, data: dict) -> db.Identity:
	issued_at, expires_at = _lifetime(data, datetime.now(timezone.utc))
	return await add_identity(
		request.app.state.db, request.app.state.secrets,
		name=_text(data, "name"),
		scheme=_text(data, "scheme"),
		username=_text(data, "username", required=False) or None,
		header=_text(data, "header", required=False) or None,   # a blank header on custom means Authorization
		issued_at=issued_at,
		expires_at=expires_at,
		rotate_url=_text(data, "rotate_url", required=False) or None,
		token=str(data.get("token") or ""),
	)


async def _rotate(request: Request, name: str, data: dict) -> db.Identity:
	issued_at, expires_at = _lifetime(data, datetime.now(timezone.utc))
	return await rotate_token(
		request.app.state.db, request.app.state.secrets, name,
		token=str(data.get("token") or ""),
		issued_at=issued_at,
		expires_at=expires_at,
		rotate_url=_text(data, "rotate_url", required=False) or None,
	)


async def _current(request: Request, identity: db.Identity) -> dict:
	registry = request.app.state.identities
	await registry.refresh()
	return _identity_dict(identity, registry, datetime.now(timezone.utc))


@router.get("/identities")
async def identities_page(request: Request) -> Response:
	return page(request, "identities.html", await _identities_payload(request))


@router.get("/api/identities")
async def identities_api(request: Request) -> Response:
	payload = await _identities_payload(request)
	return api({"identities": payload["identities"]})


@router.post("/identities")
async def identity_add_page(request: Request) -> Response:
	data = await form(request)
	try:
		identity = await _add(request, data)
	except _IDENTITY_FAILURES as exc:
		return refused(request, "identities.html", await _identities_payload(request, data), exc)
	return redirect("/_admin/identities", f"added identity '{identity.name}'; token expires {identity.expires_at:%Y-%m-%d}")


@router.post("/api/identities")
async def identity_add_api(request: Request) -> Response:
	try:
		identity = await _add(request, await form(request))
	except _IDENTITY_FAILURES as exc:
		return api_refused(exc)
	return api(await _current(request, identity), status=201)


@router.post("/identities/{name}/token")
async def identity_rotate_page(request: Request, name: str) -> Response:
	try:
		identity = await _rotate(request, name, await form(request))
	except _IDENTITY_FAILURES as exc:
		return refused(request, "identities.html", await _identities_payload(request), exc)
	return redirect("/_admin/identities", f"rotated identity '{identity.name}'; token expires {identity.expires_at:%Y-%m-%d}")


@router.post("/api/identities/{name}/token")
async def identity_rotate_api(request: Request, name: str) -> Response:
	try:
		identity = await _rotate(request, name, await form(request))
	except _IDENTITY_FAILURES as exc:
		return api_refused(exc)
	return api(await _current(request, identity))


@router.post("/identities/{name}/delete")
async def identity_delete_page(request: Request, name: str) -> Response:
	try:
		identity, had_item = await remove_identity(request.app.state.db, request.app.state.secrets, name)
	except _IDENTITY_FAILURES as exc:
		return refused(request, "identities.html", await _identities_payload(request), exc)
	message = f"deleted identity '{identity.name}'"
	if not had_item:
		message += " (note: there was no Keychain item for it)"
	return redirect("/_admin/identities", message)


@router.delete("/api/identities/{name}")
async def identity_delete_api(request: Request, name: str) -> Response:
	try:
		identity, had_item = await remove_identity(request.app.state.db, request.app.state.secrets, name)
	except _IDENTITY_FAILURES as exc:
		return api_refused(exc)
	return api({"deleted": True, "name": identity.name, "keychain_item": had_item})


# --- outbound proxies ------------------------------------------------------------
#
# A proxy password is write-only here, as a token is: the forms take one, no response carries
# one, and a refused form never echoes it (nor a URL that carried one). A blank password on an
# edit keeps the stored one.

def _proxy_dict(proxy: db.OutboundProxy, users: dict[str, list[str]], registry) -> dict:
	return {
		"name": proxy.name,
		"url": proxy.url,
		"username": proxy.username,
		"verify_tls": proxy.verify_tls,
		"ca_bundle": proxy.ca_bundle,
		"password": registry.password_state(proxy.name),
		"keychain_error": registry.load_error(proxy.name),
		"used_by": users.get(proxy.name, []),
	}


def _proxy_form(data: dict | None = None) -> dict:
	"""The add form's fields as typed: never the password, nor a URL that carried credentials."""
	data = data or {}
	url = str(data.get("url", ""))
	return {
		"name": data.get("name", ""), "url": "" if "@" in url else url,
		"username": data.get("username", ""), "ca_bundle": data.get("ca_bundle", ""),
	}


async def _proxies_payload(request: Request, data: dict | None = None) -> dict:
	conn = request.app.state.db
	registry = request.app.state.proxies
	await registry.refresh()
	users = db.upstreams_by_proxy(conn)
	return {
		"proxies": [_proxy_dict(p, users, registry) for p in db.list_proxies(conn)],
		"form": _proxy_form(data),
	}


def _proxy_fields(data: dict, current: db.OutboundProxy | None = None) -> dict:
	"""verify_tls_sent marks a page form (an unticked box is absent); a JSON body without
	verify_tls keeps the safe default. On an edit (current given), an absent username or
	ca_bundle keeps the row's value; present but empty or null clears it."""
	sent = "verify_tls" in data or data.get("verify_tls_sent")
	if current is not None and "username" not in data:
		username = current.username
	else:
		username = _text(data, "username", required=False) or None
	if current is not None and "ca_bundle" not in data:
		ca_bundle = current.ca_bundle
	else:
		ca_bundle = _text(data, "ca_bundle", required=False) or None
	return dict(
		url=_text(data, "url"),
		username=username,
		password=str(data.get("password") or ""),
		verify_tls=_checkbox(data, "verify_tls") if sent else True,
		ca_bundle=ca_bundle,
	)


async def _current_proxy(request: Request, proxy: db.OutboundProxy) -> dict:
	registry = request.app.state.proxies
	await registry.refresh()
	return _proxy_dict(proxy, db.upstreams_by_proxy(request.app.state.db), registry)


async def _add_proxy(request: Request, data: dict) -> db.OutboundProxy:
	return await add_proxy(
		request.app.state.db, request.app.state.proxy_secrets, name=_text(data, "name"), **_proxy_fields(data),
	)


async def _edit_proxy(request: Request, name: str, data: dict) -> db.OutboundProxy:
	current = db.get_proxy(request.app.state.db, name)   # None: edit_proxy raises the LookupError
	return await edit_proxy(request.app.state.db, request.app.state.proxy_secrets, name, **_proxy_fields(data, current))


@router.get("/proxies")
async def proxies_page(request: Request) -> Response:
	return page(request, "proxies.html", await _proxies_payload(request))


@router.get("/api/proxies")
async def proxies_api(request: Request) -> Response:
	return api({"proxies": (await _proxies_payload(request))["proxies"]})


@router.post("/proxies")
async def proxy_add_page(request: Request) -> Response:
	data = await form(request)
	try:
		proxy = await _add_proxy(request, data)
	except _PROXY_FAILURES as exc:
		return refused(request, "proxies.html", await _proxies_payload(request, data), exc)
	return redirect("/_admin/proxies", f"added proxy '{proxy.name}'")


@router.post("/api/proxies")
async def proxy_add_api(request: Request) -> Response:
	try:
		proxy = await _add_proxy(request, await form(request))
	except _PROXY_FAILURES as exc:
		return api_refused(exc)
	return api(await _current_proxy(request, proxy), status=201)


@router.post("/proxies/{name}")
async def proxy_edit_page(request: Request, name: str) -> Response:
	try:
		proxy = await _edit_proxy(request, name, await form(request))
	except _PROXY_FAILURES as exc:
		return refused(request, "proxies.html", await _proxies_payload(request), exc)
	return redirect("/_admin/proxies", f"saved proxy '{proxy.name}'")


@router.put("/api/proxies/{name}")
async def proxy_edit_api(request: Request, name: str) -> Response:
	try:
		proxy = await _edit_proxy(request, name, await form(request))
	except _PROXY_FAILURES as exc:
		return api_refused(exc)
	return api(await _current_proxy(request, proxy))


@router.post("/proxies/{name}/delete")
async def proxy_delete_page(request: Request, name: str) -> Response:
	try:
		proxy, had_item = await remove_proxy(request.app.state.db, request.app.state.proxy_secrets, name)
	except _PROXY_FAILURES as exc:
		return refused(request, "proxies.html", await _proxies_payload(request), exc)
	message = f"deleted proxy '{proxy.name}'"
	if proxy.username and not had_item:
		message += " (note: there was no Keychain item for it)"
	return redirect("/_admin/proxies", message)


@router.delete("/api/proxies/{name}")
async def proxy_delete_api(request: Request, name: str) -> Response:
	try:
		proxy, had_item = await remove_proxy(request.app.state.db, request.app.state.proxy_secrets, name)
	except _PROXY_FAILURES as exc:
		return api_refused(exc)
	return api({"deleted": True, "name": proxy.name, "keychain_item": had_item})


# --- approvals ----------------------------------------------------------------------

def _decision(value) -> Decision:
	text = str(value or "").strip()
	for decision in Decision:
		if text in (decision.value, decision.name):
			return decision
	raise ValueError(f"decision must be one of {[d.value for d in Decision]}: {text!r}")


def _identity_id(data: dict) -> int:
	try:
		return int(data.get("identity_id"))
	except (TypeError, ValueError):
		raise ValueError(f"identity_id must be an integer: {data.get('identity_id')!r}") from None


def _approvals_payload(request: Request) -> dict:
	conn = request.app.state.db
	names = {identity.id: identity.name for identity in db.list_identities(conn)}

	def named(row: dict) -> dict:
		return {**row, "identity": names.get(row["identity_id"], f"deleted identity #{row['identity_id']}")}

	snapshot = request.app.state.approvals.snapshot()
	return {
		"pending": [named(row) for row in snapshot["pending"]],
		"always": [
			{"client_addr": a.client_addr, "identity": a.identity, "granted_at": _iso(a.granted_at)}
			for a in db.list_approvals(conn)
		],
		"grants": [named(row) for row in snapshot["grants"]],
		"cooldowns": [named(row) for row in snapshot["cooldowns"]],
		"decisions": [d.value for d in Decision],
	}


@router.get("/approvals")
async def approvals_page(request: Request) -> Response:
	return page(request, "approvals.html", _approvals_payload(request))


@router.get("/api/approvals")
async def approvals_api(request: Request) -> Response:
	return api(_approvals_payload(request))


@router.get("/approvals/pending")
async def approvals_pending_partial(request: Request) -> Response:
	"""The pending section alone; the page polls it every two seconds."""
	return page(request, "approvals_pending.html", _approvals_payload(request))


def _answer(request: Request, data: dict) -> None:
	client_addr, identity_id, decision = _text(data, "client_addr"), _identity_id(data), _decision(data.get("decision"))
	if not request.app.state.approvals.answer(client_addr, identity_id, decision):
		raise LookupError(f"nothing is pending for {client_addr} and identity #{identity_id}; the dialog may have answered first")


@router.post("/approvals/answer")
async def approvals_answer_page(request: Request) -> Response:
	try:
		_answer(request, await form(request))
	except _REFUSALS as exc:   # the partial again, with the notice; base.html tells htmx to swap 4xx too
		return page(request, "approvals_pending.html", {**_approvals_payload(request), "notice": str(exc)}, status=_status_for(exc))
	return page(request, "approvals_pending.html", _approvals_payload(request))


@router.post("/api/approvals/answer")
async def approvals_answer_api(request: Request) -> Response:
	try:
		_answer(request, await form(request))
	except _REFUSALS as exc:
		return api_refused(exc)
	return api({"answered": True})


def _revoke_always(request: Request, data: dict) -> str:
	client_addr, identity = _text(data, "client_addr"), _text(data, "identity")
	if not db.delete_approval(request.app.state.db, client_addr, identity):
		raise LookupError(f"no Always approval for {client_addr} using {identity!r}")
	return f"revoked: {client_addr} using '{identity}' will be asked again"


def _revoke_grant(request: Request, data: dict) -> str:
	client_addr, identity_id = _text(data, "client_addr"), _identity_id(data)
	if not request.app.state.approvals.revoke_grant(client_addr, identity_id):
		raise LookupError(f"no One hour or Session grant for {client_addr} and identity #{identity_id}")
	return f"revoked: {client_addr} will be asked again"


def _clear_cooldown(request: Request, data: dict) -> str:
	client_addr, identity_id = _text(data, "client_addr"), _identity_id(data)
	if not request.app.state.approvals.clear_cooldown(client_addr, identity_id):
		raise LookupError(f"no cooldown for {client_addr} and identity #{identity_id}")
	return f"cleared: {client_addr} may ask again"


async def _approval_action_page(request: Request, action) -> Response:
	try:
		message = action(request, await form(request))
	except _REFUSALS as exc:
		return refused(request, "approvals.html", _approvals_payload(request), exc)
	return redirect("/_admin/approvals", message)


async def _approval_action_api(request: Request, action, key: str) -> Response:
	try:
		action(request, await form(request))
	except _REFUSALS as exc:
		return api_refused(exc)
	return api({key: True})


@router.post("/approvals/always/revoke")
async def approvals_revoke_always_page(request: Request) -> Response:
	return await _approval_action_page(request, _revoke_always)


@router.post("/api/approvals/always/revoke")
async def approvals_revoke_always_api(request: Request) -> Response:
	return await _approval_action_api(request, _revoke_always, "revoked")


@router.post("/approvals/grants/revoke")
async def approvals_revoke_grant_page(request: Request) -> Response:
	return await _approval_action_page(request, _revoke_grant)


@router.post("/api/approvals/grants/revoke")
async def approvals_revoke_grant_api(request: Request) -> Response:
	return await _approval_action_api(request, _revoke_grant, "revoked")


@router.post("/approvals/cooldowns/clear")
async def approvals_clear_cooldown_page(request: Request) -> Response:
	return await _approval_action_page(request, _clear_cooldown)


@router.post("/api/approvals/cooldowns/clear")
async def approvals_clear_cooldown_api(request: Request) -> Response:
	return await _approval_action_api(request, _clear_cooldown, "cleared")


# --- test a URL ------------------------------------------------------------------

def _route_label(conn, upstream: db.Upstream) -> str:
	if upstream.proxy is None:
		return "Direct"
	proxy = db.get_proxy(conn, upstream.proxy)
	return f"via {proxy.name} ({proxy.url})"


async def _test_payload(request: Request, path: str, query: str, identity_name: str, requested: str) -> dict:
	"""Read-only: which upstream the request would reach (as X-WR-Upstream, or inferred from the
	identity, would choose), the matched rules ranked, the key, and fresh/stale/absent.
	Nothing is fetched or written."""
	conn = request.app.state.db
	if not path.startswith("/"):
		raise ValueError(f"path must start with '/': {path!r}")
	identity = None
	partition = ANONYMOUS
	if identity_name and identity_name != ANONYMOUS:
		identity = db.get_identity(conn, identity_name)
		if identity is None:
			raise ValueError(f"unknown identity {identity_name!r}")
		partition = identity.cache_identity
	try:
		upstream, refusal = choose_upstream(conn, requested or None, identity), None
	except RoutingRefusal as exc:
		upstream, refusal = None, exc
	matches = request.app.state.rules.get().match_all(upstream.upstream_id, path) if upstream else []
	winner = matches[0] if matches else None
	payload = {
		"path": path, "query": query, "identity": identity_name or ANONYMOUS,
		"upstream": upstream.upstream_id if upstream else None,
		"route": _route_label(conn, upstream) if upstream else None,
		"matches": [
			{"id": r.id, "pattern": r.pattern, "ttl": r.ttl_seconds,
			 "literals": specificity(r.pattern)[0], "wildcards": specificity(r.pattern)[1]}
			for r in matches
		],
		"outcome": None, "key": None, "state": None, "age": None,
	}
	if refusal is not None:
		payload["outcome"] = f"the request would get a {refusal.status}: {refusal}"
	elif winner is None:
		payload["outcome"] = "no rule matches; the request would pass through uncached"
	elif winner.bypass:
		payload["outcome"] = f"rule {winner.pattern} is a bypass; the request would pass through uncached"
	elif winner.query_mode == "reject" and query:
		payload["outcome"] = "the winning rule rejects queries; this request would pass through uncached"
	else:
		key = build_key(partition, path, normalize_query(query, winner.query_mode))
		entry = await request.app.state.cache.get(upstream.upstream_id, key)
		payload["key"] = key
		if entry is None:
			payload["state"] = "absent"
		elif is_fresh(entry, ttl=winner.ttl_seconds):
			payload["state"], payload["age"] = "fresh", age_seconds(entry)
		else:
			payload["state"], payload["age"] = "stale", age_seconds(entry)
		payload["outcome"] = f"cacheable under rule {winner.pattern} ({ttl_human(winner.ttl_seconds)})"
	return payload


def _test_form(request: Request) -> dict:
	q = request.query_params
	conn = request.app.state.db
	return {
		"identities": [ANONYMOUS, *(i.name for i in db.list_identities(conn))],
		"upstreams": [u.upstream_id for u in db.list_upstreams(conn)],
		"form": {
			"path": q.get("path", ""), "query": q.get("query", ""), "identity": q.get("identity", ANONYMOUS),
			"upstream": q.get("upstream", ""),
		},
	}


@router.get("/test")
async def test_url_page(request: Request) -> Response:
	q = request.query_params
	payload = {**_test_form(request), "result": None}
	if q.get("path"):
		try:
			payload["result"] = await _test_payload(
				request, q["path"], q.get("query", ""), q.get("identity", ""), q.get("upstream", ""),
			)
		except _REFUSALS as exc:
			return refused(request, "test_url.html", payload, exc)
	return page(request, "test_url.html", payload)


@router.get("/api/test")
async def test_url_api(request: Request) -> Response:
	q = request.query_params
	try:
		return api(await _test_payload(
			request, q.get("path", ""), q.get("query", ""), q.get("identity", ""), q.get("upstream", ""),
		))
	except _REFUSALS as exc:
		return api_refused(exc)


@router.post("/rules/{rule_id}/toggle")
async def rule_toggle(request: Request, rule_id: int) -> Response:
	"""Flip enabled; answers with the one table row, which HTMX swaps in place."""
	try:
		rule = _get_rule_or_404(request, rule_id)
		rule = db.update_rule(
			request.app.state.db, rule_id, upstream_id=rule.upstream_id, pattern=rule.pattern, ttl_seconds=rule.ttl_seconds,
			query_mode=rule.query_mode, enabled=not rule.enabled, note=rule.note,
		)
	except _REFUSALS as exc:
		return api_refused(exc)
	hits = await request.app.state.cache.hits_by_rule()
	return page(request, "rule_row.html", {"rule": _rule_dict(rule), "hits": hits})
