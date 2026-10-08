"""HTTP server: one process serving A2A, MCP, REST, the agent card, deliverables
and a status page, plus background workers (ERC-8183 watcher, keeper)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any

from aiohttp import web

from . import ENGINE_NAME, ENGINE_VERSION, __version__
from .a2a import A2AHandler, rpc_error
from .card import CATEGORY, DESCRIPTION, NAME, TAGLINE, agent_card, price_atomic, registration_file
from .chain import load_account
from .config import Config
from .db import DB
from .keeper import Keeper
from .mcp import MCPHandler
from .rpc import RpcPool, pool_from_config
from .seller import Seller
from .service import HFService

log = logging.getLogger("vitals.server")
JSON_CT = "application/json"
ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#12241b"/>'
    '<path d="M6 34h14l5-12 8 24 6-16 4 4h15" fill="none" stroke="#5ee39a" stroke-width="4" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>'
)


def jresp(data: Any, status: int = 200, headers: dict | None = None) -> web.Response:
    return web.Response(text=json.dumps(data, default=_default, separators=(",", ":")), status=status,
                        content_type=JSON_CT, headers=headers)


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return "0x" + o.hex()
    return str(o)


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        if len(self.hits) > 20_000:
            self.hits.clear()
        return True


def client_ip(request: web.Request) -> str:
    peer = request.remote or "unknown"
    if peer in ("127.0.0.1", "::1"):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[-1].strip() or peer
    return peer


class App:
    def __init__(self, cfg: Config, *, pool: RpcPool | None = None, write_pool: RpcPool | None = None):
        self.cfg = cfg
        self.pool = pool or pool_from_config(cfg)
        self.write_pool = write_pool or RpcPool(cfg.rpc_write or cfg.rpc_head, cfg.rpc_archive,
                                                logs=cfg.rpc_logs, timeout=cfg.rpc_timeout, retries=cfg.rpc_retries)
        self.db = DB(cfg.db_path)
        self.hf = HFService(cfg, self.pool)
        self.account = load_account(cfg)
        self.seller = Seller(cfg, self.pool, self.db, self.hf, self.account, write_pool=self.write_pool)
        self.keeper = Keeper(cfg, self.pool, self.db, self.account, write_pool=self.write_pool)
        self.a2a = A2AHandler(self.hf.answer, self.seller)
        self.mcp = MCPHandler(self.hf.answer, self.info)
        self.limiter = RateLimiter(cfg.rate_limit_per_minute)
        self.started = time.time()
        self.keeper_cache: dict | None = None
        self.keeper_cache_at = 0.0
        self.sem = asyncio.Semaphore(8)

    # ------------------------------------------------------------ helpers

    def info(self) -> dict:
        return {
            "name": NAME, "tagline": TAGLINE, "category": CATEGORY, "version": __version__,
            "engine": {"name": ENGINE_NAME, "version": ENGINE_VERSION},
            "agentId": self.cfg.agent_id, "chainId": self.cfg.chain_id,
            "agentRegistry": f"eip155:{self.cfg.chain_id}:{self.cfg.identity_registry}",
            "owner": self.cfg.owner, "provider": self.seller.address,
            "price": {"amount": str(self.cfg.price_u), "currency": "U", "token": self.cfg.payment_token,
                      "atomic": str(price_atomic(self.cfg))},
            "hire": {"skill": "negotiate-erc8183-job", "a2a": self.cfg.a2a_url, "commerce": self.cfg.commerce,
                     "router": self.cfg.router, "policy": self.cfg.policy},
            "card": self.cfg.card_url, "mcp": self.cfg.mcp_url, "github": self.cfg.github_url,
        }

    def endpoints(self) -> dict:
        b = self.cfg.base_url
        return {
            "GET /": "status page (HTML; JSON with Accept: application/json)",
            "GET /.well-known/agent-card.json": "A2A agent card (alias /.well-known/agent.json)",
            "GET /.well-known/agent-registration.json": "ERC-8004 registration file",
            "POST /a2a": "A2A JSON-RPC 2.0 (message/send, tasks/get)",
            "POST /mcp": "MCP streamable HTTP (tool venus_health_factor)",
            "GET|POST /api/hf": "REST health-factor report: ?address=0x..&block=N&target=2.0",
            "POST /api/negotiate": "ERC-8183 quote (same as the negotiate-erc8183-job skill)",
            "GET /api/jobs": "ERC-8183 jobs served", "GET /api/jobs/{id}": "one job",
            "GET /api/keeper/status": "own-position keeper status",
            "GET /deliverables/{hash}.json": "content-addressed deliverables",
            "GET /health": "liveness", "base": b,
        }

    async def blocking(self, fn, *args, **kwargs):
        async with self.sem:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ------------------------------------------------------------- routes

    async def index(self, request: web.Request) -> web.Response:
        accept = request.headers.get("Accept", "")
        status = await self.keeper_status_cached()
        jobs = self.db.job_counts()
        if JSON_CT in accept and "text/html" not in accept:
            return jresp({**self.info(), "description": DESCRIPTION, "endpoints": self.endpoints(),
                          "jobs": jobs, "keeper": _keeper_brief(status), "uptimeSeconds": int(time.time() - self.started)})
        e = html.escape
        agent = "pending" if self.cfg.agent_id is None else str(self.cfg.agent_id)
        hf = status.get("healthFactor")
        acts = "".join(
            f"<li>{time.strftime('%Y-%m-%d %H:%M', time.gmtime(a['ts']))} UTC: {e(a['action'])} {e(a['amount'] or '')}"
            f" USDT ({e(a['status'])}{', dry run' if a['dryRun'] else ''})"
            + (f' <a href="{e(a["bscscan"])}">tx</a>' if a.get("bscscan") else "") + "</li>"
            for a in status.get("lastActions", [])[:6]
        ) or "<li>no keeper actions yet</li>"
        served = sum(v for k, v in jobs.items() if k in ("submitted", "settling", "completed"))
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{NAME}: {e(TAGLINE)}</title>
<link rel="icon" href="/icon.svg"><style>
body{{font:16px/1.55 system-ui,sans-serif;max-width:720px;margin:2.5rem auto;padding:0 1rem;color:#15160f;background:#f6f6f1}}
h1{{font-size:1.9rem;margin:.2rem 0}} code{{background:#e9e9e2;padding:.1rem .3rem;border-radius:4px;font-size:.92em}}
.k{{color:#5c5e52}} section{{border-top:1px solid #d4d4cb;margin-top:1.4rem;padding-top:.6rem}} a{{color:#2b5a85}}
</style></head><body>
<p class="k">Category: health factor · BNB Smart Chain (56) · Venus Core Pool</p>
<h1>{NAME}</h1><p><strong>{e(TAGLINE)}.</strong> {e(DESCRIPTION)}</p>
<section><h2>Identity</h2><p>ERC-8004 agent ID: <code>{agent}</code> · Registry
<code>{e(self.cfg.identity_registry)}</code> on chain {self.cfg.chain_id} · Owner <code>{e(self.cfg.owner)}</code></p>
<p><a href="/.well-known/agent-card.json">Agent card</a> · <a href="/.well-known/agent-registration.json">Registration file</a>
· <a href="{e(self.cfg.github_url)}">Source on GitHub</a></p></section>
<section><h2>Hire</h2><p>Free: A2A <code>POST /a2a</code>, MCP <code>POST /mcp</code>, REST <code>GET /api/hf?address=0x…</code>.
Paid: ERC-8183 job at {e(str(self.cfg.price_u))} U via the <code>negotiate-erc8183-job</code> skill; Vitals delivers a
content-addressed JSON report and settles after the review window. Jobs served: {served}
(<a href="/api/jobs">list</a>).</p></section>
<section><h2>Own position (keeper)</h2><p>Health factor: <strong>{'n/a' if hf is None else f'{hf:.4f}'}</strong>
· mode: {'live' if status.get('live') else 'dry run'} · <a href="/api/keeper/status">status JSON</a></p><ul>{acts}</ul></section>
<section class="k"><p>{ENGINE_NAME} {ENGINE_VERSION} · every number is read on chain at one block and reconciles with
Comptroller.getAccountLiquidity.</p></section></body></html>"""
        return web.Response(text=page, content_type="text/html")

    async def icon(self, request: web.Request) -> web.Response:
        return web.Response(text=ICON, content_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    async def card(self, request: web.Request) -> web.Response:
        return jresp(agent_card(self.cfg, provider_address=self.seller.address),
                     headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=60"})

    async def registration(self, request: web.Request) -> web.Response:
        return jresp(registration_file(self.cfg), headers={"Access-Control-Allow-Origin": "*"})

    async def health(self, request: web.Request) -> web.Response:
        return jresp({"ok": True, "name": NAME, "version": __version__, "uptimeSeconds": int(time.time() - self.started),
                      "watcherLastTick": self.seller.last_tick, "signer": self.seller.can_sign,
                      "signerMatchesOwner": self.seller.signer_matches_owner, "live": self.cfg.live})

    async def a2a_get(self, request: web.Request) -> web.Response:
        from .card import EXAMPLE_ADDRESS
        return jresp({
            "endpoint": self.cfg.a2a_url, "url": self.cfg.a2a_url, "protocol": "A2A 0.3.0 JSON-RPC 2.0 over HTTP POST",
            "methods": ["message/send", "tasks/get", "tasks/cancel"], "card": self.cfg.card_url,
            "skills": ["venus-health-factor", "negotiate-erc8183-job", "negotiate", "notify_funded",
                       "erc8183-job-status"],
            "example": {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
                "role": "user", "messageId": "example-1",
                "parts": [{"kind": "text", "text": f"Account: {EXAMPLE_ADDRESS}. Block: 124010796. Report the "
                           "health factor and the USD of debt to repay to restore a health factor of 2.5."}]}}},
        })

    async def a2a_post(self, request: web.Request) -> web.Response:
        try:
            body = json.loads(await request.text())
        except ValueError:
            return jresp(rpc_error(None, -32700, "Parse error: body is not JSON"), status=400)
        if not self.limiter.allow(client_ip(request)):
            return jresp(rpc_error(body.get("id") if isinstance(body, dict) else None, -32000,
                                   "Rate limited: retry in a minute"), status=429)
        result = await self.blocking(self.a2a.handle, body, client=client_ip(request))
        if result is None:
            return web.Response(status=204)
        return jresp(result)

    async def mcp_get(self, request: web.Request) -> web.Response:
        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept and JSON_CT not in accept:
            return jresp({"error": "this MCP server does not open a server-to-client SSE stream; POST JSON-RPC to "
                          "this URL"}, status=405, headers={"Allow": "POST, DELETE"})
        from .mcp import HF_TOOL, SUPPORTED_VERSIONS
        return jresp({"endpoint": self.cfg.mcp_url, "transport": "MCP streamable HTTP (JSON responses)",
                      "protocolVersions": list(SUPPORTED_VERSIONS), "tools": [HF_TOOL["name"], "vitals_agent_info"],
                      "usage": "POST initialize, then tools/list and tools/call with Accept: application/json, "
                               "text/event-stream"})

    async def mcp_post(self, request: web.Request) -> web.Response:
        try:
            body = json.loads(await request.text())
        except ValueError:
            return jresp({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status=400)
        if not self.limiter.allow(client_ip(request)):
            return jresp({"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                          "error": {"code": -32000, "message": "Rate limited"}}, status=429)
        result, headers = await self.blocking(self.mcp.handle, body)
        if result is None:
            return web.Response(status=202, headers=headers)
        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept and JSON_CT not in accept:
            text = f"event: message\ndata: {json.dumps(result, default=_default)}\n\n"
            return web.Response(text=text, content_type="text/event-stream", headers=headers)
        return jresp(result, headers=headers)

    async def mcp_delete(self, request: web.Request) -> web.Response:
        return jresp({"ok": True, "note": "session closed"})

    async def api_index(self, request: web.Request) -> web.Response:
        return jresp({"name": NAME, "endpoints": self.endpoints()})

    async def api_hf(self, request: web.Request) -> web.Response:
        data: dict[str, Any] = {}
        text = None
        if request.method == "POST":
            raw = await request.text()
            try:
                parsed = json.loads(raw) if raw.strip() else {}
                if isinstance(parsed, dict):
                    data = parsed
                elif isinstance(parsed, str):
                    text = parsed
            except ValueError:
                text = raw
        q = request.query
        for src, dst in (("address", "address"), ("account", "address"), ("block", "blockNumber"),
                         ("blockNumber", "blockNumber"), ("target", "targetHealthFactor"),
                         ("targetHealthFactor", "targetHealthFactor")):
            if src in q and dst not in data:
                data[dst] = q[src]
        if request.method == "GET" and not data and not text:
            from .card import EXAMPLE_ADDRESS
            return jresp({"endpoint": f"{self.cfg.base_url}/api/hf", "method": "GET or POST",
                          "params": {"address": "0x… (required)", "block": "block number (default latest)",
                                     "target": "target health factor (default 2.0)"},
                          "example": f"{self.cfg.base_url}/api/hf?address={EXAMPLE_ADDRESS}&block=124010796&target=2.5"})
        if not self.limiter.allow(client_ip(request)):
            return jresp({"error": "rate limited"}, status=429)
        ok, payload = await self.blocking(self.hf.answer, text, data)
        return jresp(payload, status=200 if ok else (503 if payload.get("retryable") else 400))

    async def api_negotiate(self, request: web.Request) -> web.Response:
        if request.method == "GET":
            return jresp({"endpoint": f"{self.cfg.base_url}/api/negotiate", "method": "POST",
                          "body": {"task_description": "text or JSON naming a 0x Venus account",
                                   "terms": {"deliverables": "...", "quality_standards": "..."}},
                          "returns": "bnbagent SDK NegotiationResult (signed quote)"})
        try:
            data = json.loads(await request.text())
        except ValueError:
            return jresp({"error": "body must be JSON"}, status=400)
        env = await self.blocking(self.seller.negotiate, data if isinstance(data, dict) else {}, client_ip(request))
        return jresp(env)

    async def api_jobs(self, request: web.Request) -> web.Response:
        return jresp({"provider": self.seller.address, "counts": self.db.job_counts(),
                      "jobs": self.seller.public_jobs(), "quotesIssued": self.db.quote_count()})

    async def api_job(self, request: web.Request) -> web.Response:
        try:
            jid = int(request.match_info["job_id"])
        except ValueError:
            return jresp({"error": "job id must be an integer"}, status=400)
        st = await self.blocking(self.seller.job_status, jid)
        return jresp(st, status=404 if st.get("error") == "job not found" else 200)

    async def keeper_status_cached(self, max_age: float = 120.0) -> dict:
        if self.keeper_cache is None or time.time() - self.keeper_cache_at > max_age:
            try:
                self.keeper_cache = await asyncio.wait_for(self.blocking(self.keeper.status), timeout=20)
            except Exception as exc:
                self.keeper_cache = {"error": f"{type(exc).__name__}", "lastActions": []}
            self.keeper_cache_at = time.time()
        return self.keeper_cache

    async def keeper_status(self, request: web.Request) -> web.Response:
        return jresp(await self.keeper_status_cached(max_age=30))

    async def deliverable(self, request: web.Request) -> web.Response:
        h = request.match_info["hash"].lower()
        try:
            raw = self.seller.store.read_bytes(h)
        except ValueError:
            return jresp({"error": "not a deliverable hash"}, status=400)
        if raw is None:
            return jresp({"error": "deliverable not found", "hash": h}, status=404)
        return web.Response(body=raw, content_type=JSON_CT, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # ------------------------------------------------------------ workers

    async def watcher_loop(self) -> None:
        await asyncio.to_thread(self.seller.reconcile_on_start)
        n = 0
        while True:
            try:
                summary = await asyncio.to_thread(self.seller.tick, logs=(n % 4 == 0))
                if summary.get("delivered") or summary.get("settled") or summary.get("new"):
                    log.info("watcher: %s", json.dumps(summary, default=str)[:500])
            except Exception as exc:
                self.seller.last_error = str(exc)
                log.warning("watcher tick failed: %s", exc)
            n += 1
            woke = await asyncio.to_thread(self.seller.wake.wait, self.cfg.watch_interval)
            if woke:
                self.seller.wake.clear()

    async def keeper_loop(self) -> None:
        while True:
            try:
                res = await asyncio.to_thread(self.keeper.run_if_due)
                if res is not None:
                    log.info("keeper: %s", json.dumps(res, default=str)[:500])
                    self.keeper_cache = None
            except Exception as exc:
                log.warning("keeper loop failed: %s", exc)
            await asyncio.sleep(60)

    async def on_startup(self, app: web.Application) -> None:
        self._tasks = []
        if self.cfg.watch_enabled and self.seller.can_sign:
            self._tasks.append(asyncio.create_task(self.watcher_loop()))
        if self.cfg.keeper_enabled and self.account is not None:
            self._tasks.append(asyncio.create_task(self.keeper_loop()))

    async def on_cleanup(self, app: web.Application) -> None:
        for t in getattr(self, "_tasks", []):
            t.cancel()
        self.db.close()


@web.middleware
async def errors(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPMethodNotAllowed as exc:
        if request.method in ("GET", "HEAD"):
            return jresp({"path": request.path, "allowed": sorted(exc.allowed_methods),
                          "note": "POST JSON to this endpoint; GET / lists every endpoint"})
        return jresp({"error": "method not allowed", "allowed": sorted(exc.allowed_methods)}, status=405)
    except web.HTTPNotFound:
        return jresp({"error": "not found", "path": request.path, "see": "/api"}, status=404)
    except web.HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - last resort
        log.exception("unhandled error on %s", request.path)
        return jresp({"error": "internal error", "type": type(exc).__name__}, status=500)
    resp.headers.setdefault("X-Agent", f"{NAME}/{__version__}")
    return resp


def _keeper_brief(status: dict) -> dict:
    return {k: status.get(k) for k in ("healthFactor", "live", "lastTxAgeHours")}


def build_app(cfg: Config | None = None, **kwargs) -> web.Application:
    cfg = cfg or Config.from_env()
    core = App(cfg, **kwargs)
    app = web.Application(middlewares=[errors], client_max_size=256 * 1024)
    app["core"] = core
    r = app.router
    r.add_get("/", core.index)
    r.add_post("/", core.a2a_post)
    r.add_get("/icon.svg", core.icon)
    for p in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        r.add_get(p, core.card)
        r.add_post(p, core.a2a_post)
    r.add_get("/.well-known/agent-registration.json", core.registration)
    r.add_get("/health", core.health)
    r.add_get("/healthz", core.health)
    r.add_get("/a2a", core.a2a_get)
    r.add_post("/a2a", core.a2a_post)
    r.add_get("/mcp", core.mcp_get)
    r.add_post("/mcp", core.mcp_post)
    r.add_delete("/mcp", core.mcp_delete)
    r.add_get("/api", core.api_index)
    r.add_route("*", "/api/hf", core.api_hf)
    r.add_route("*", "/api/negotiate", core.api_negotiate)
    r.add_get("/api/jobs", core.api_jobs)
    r.add_get("/api/jobs/{job_id}", core.api_job)
    r.add_get("/api/keeper/status", core.keeper_status)
    r.add_get("/deliverables/{hash}.json", core.deliverable)
    app.on_startup.append(core.on_startup)
    app.on_cleanup.append(core.on_cleanup)
    return app


def serve(cfg: Config | None = None) -> None:
    cfg = cfg or Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port, access_log=None)
