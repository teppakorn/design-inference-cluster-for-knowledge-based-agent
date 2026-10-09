"""pd-gateway: the one OpenAI-compatible endpoint in front of the vLLM prefill/decode pods.

Every request goes through three stages, one file each:
    admission.py  accept or turn away: token bucket per tenant, fleet saturation, SLO deadline
    queue.py      wait for a free decode slot; QUEUE_POLICY picks who goes next (default two-class)
    router.py     pick a decode pod and a prefill pod (ROUTER_POLICY, default prefix-load)
Then the request is sent to the chosen decode pod's llm-d routing sidecar with the header
`x-prefiller-host-port: <prefill pod>`. The sidecar runs the prefill leg on that prefill pod
(max_tokens=1, kv_transfer_params.do_remote_decode), the prefill vLLM pushes the prompt's KV into the
decode pod's GPU memory (NIXL over CUDA IPC), and the decode vLLM streams the answer back.

The gateway streams the answer through and times every request itself: TTFT (first content, reasoning
or tool-call delta, counted from arrival, so it includes the queue wait), gaps between chunks, E2E, and
token usage (incl. cached tokens). Prometheus metrics on /metrics; one JSON line per request in
$TRACE_DIR/gateway.jsonl when TRACE_DIR is set.

Request headers the gateway reads (all optional):
    x-tenant      tenant id: token bucket, tenant round-robin in the queue, CACHE_SALT=tenant
                  (TENANT_HEADERS: the first of these headers that is set; Open WebUI sends x-openwebui-user-id)
    x-slo-s       this request's deadline in seconds (default SLO_S)
    x-request-id  passed on to vLLM and written to the trace
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from gateway.admission import Admission
from gateway.queue import QUEUE_POLICY, Queue, Ticket, estimate_tokens
from gateway.router import POLICY, Pod, Pool, prefix_hashes

# sampling defaults added to every request that does not set them (config.sh SAMPLING_DEFAULTS_JSON)
SAMPLING_DEFAULTS = json.loads(os.environ.get("SAMPLING_DEFAULTS") or "{}")
PREFILL_DNS = os.environ.get("PREFILL_DNS", "vllm-prefill-pods:8000")
DECODE_DNS = os.environ.get("DECODE_DNS", "vllm-decode-pods:8000")
DECODE_METRICS_PORT = int(os.environ.get("DECODE_METRICS_PORT", "8200"))  # decode vLLM itself, behind the sidecar
DISCOVERY_S = float(os.environ.get("DISCOVERY_INTERVAL_S", "5"))
# CACHE_SALT=tenant: each tenant gets its own prefix cache. The gateway sets cache_salt = "tenant:<x-tenant>"
# on every request (over any salt the client sent); vLLM folds it into its block hashes, which splits the GPU
# prefix cache of prefill and decode and the Mooncake keys; the router's prefix index follows the salt.
# It splits what can be reused, not the memory: KV space and the Mooncake pool stay shared.
CACHE_SALT = os.environ.get("CACHE_SALT", "off")
if CACHE_SALT not in ("off", "tenant"):
    raise SystemExit(f"unknown CACHE_SALT {CACHE_SALT!r} (off | tenant)")
TENANT_HEADERS = [h.strip() for h in os.environ.get("TENANT_HEADERS", "x-tenant,x-openwebui-user-id").split(",") if h.strip()]
TRACE_DIR = os.environ.get("TRACE_DIR", "")
PORT = int(os.environ.get("SERVE_PORT", "8080"))

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 7.5, 10, 15, 20, 30, 45, 60, 90, 120)
ITL_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075, 0.1, 0.2, 0.5, 1, 2, 5)

REQS = Counter("gw_requests_total", "Requests by HTTP status", ["code"])
ROUTED = Counter("gw_routed_total", "Requests routed to a pod", ["role", "pod"])
INFLIGHT = Gauge("gw_inflight", "In-flight requests per pod (prefill: until the first token)", ["role", "pod"])
TTFT = Histogram("gw_ttft_seconds", "Time to first token at the gateway (incl. queue wait)", buckets=BUCKETS)
E2E = Histogram("gw_e2e_seconds", "Request latency at the gateway", buckets=BUCKETS)
ITL = Histogram("gw_itl_seconds", "Gap between streamed chunks", buckets=ITL_BUCKETS)
PREFIX = Histogram("gw_prefix_match_ratio", "Router prefix match of the chosen pod", ["role"],
                   buckets=(0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0))
TOKENS = Counter("gw_tokens_total", "Token usage reported by vLLM", ["kind"])
for _code in ("200", "429", "502", "503"):  # exist at 0 from the start, so rate() also sees the first one
    REQS.labels(_code)

Placed = list[tuple[str, Pod, float]]  # (role, pod, prefix match) the request is counted on


def metrics_url(role: str, pod: Pod) -> str:
    """Where a pod's vLLM /metrics lives (the decode vLLM listens behind the llm-d sidecar)."""
    return f"http://{pod.ip}:{DECODE_METRICS_PORT if role == 'decode' else pod.port}/metrics"


class Gateway:
    def __init__(self) -> None:
        self.pools = {"prefill": Pool.from_spec("prefill", PREFILL_DNS), "decode": Pool.from_spec("decode", DECODE_DNS)}
        self.queue = Queue(self.pools["decode"])
        self.admission = Admission(self.pools, self.queue, metrics_url)
        self.session: ClientSession | None = None
        self.trace_path = Path(TRACE_DIR) / "gateway.jsonl" if TRACE_DIR else None
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace_q: asyncio.Queue[dict] = asyncio.Queue()

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(
            connector=TCPConnector(limit=0, keepalive_timeout=30, ttl_dns_cache=None),
            timeout=ClientTimeout(total=None, sock_connect=10, sock_read=900),
        )
        await self.discover()
        app["tasks"] = [asyncio.create_task(self._discover_loop()), asyncio.create_task(self.admission.fleet_loop(self.session))]
        if self.trace_path:
            app["tasks"].append(asyncio.create_task(self._trace_writer()))

    async def stop(self, app: web.Application) -> None:
        for t in app.get("tasks", []):
            t.cancel()
        if self.session:
            await self.session.close()

    async def discover(self) -> None:
        for pool in self.pools.values():
            await pool.discover()
        self.queue.kick()  # new pods = new slots

    async def _discover_loop(self) -> None:
        while True:
            await asyncio.sleep(DISCOVERY_S)
            try:
                await self.discover()
            except Exception as exc:  # noqa: BLE001
                print(f"discovery error: {exc!r}", flush=True)

    async def _trace_writer(self) -> None:
        with self.trace_path.open("a", encoding="utf-8") as fh:
            while True:
                rec = await self.trace_q.get()
                fh.write(json.dumps(rec) + "\n")
                if self.trace_q.empty():
                    fh.flush()

    def trace(self, rec: dict) -> None:
        if self.trace_path:
            self.trace_q.put_nowait(rec)

    def release(self, placed: Placed, role_filter: str | None = None) -> None:
        for role, p, _ in placed:
            if role_filter and role != role_filter:
                continue
            p.inflight -= 1
            INFLIGHT.labels(role, p.name).set(p.inflight)
        if role_filter != "prefill":  # a decode slot came free
            self.queue.kick()

    # ---------------------------------------------------------------- handlers
    async def models(self, request: web.Request) -> web.Response:
        pod = next(iter(self.pools["decode"].pods.values()), None)
        if pod is None:
            return web.json_response({"error": {"message": "no ready decode pod"}}, status=503)
        async with self.session.get(f"http://{pod.addr}/v1/models") as r:
            return web.Response(body=await r.read(), status=r.status, content_type="application/json")

    async def debug_pods(self, request: web.Request) -> web.Response:
        return web.json_response({
            "router": POLICY,
            "queue": self.queue.state(),
            "admission": self.admission.state()["levels"],
            "slo_s": self.admission.slo_s,
            "cache_salt": CACHE_SALT,
            "pools": {k: [{"pod": p.name, "addr": p.addr, "inflight": p.inflight, "total": p.total}
                          for p in v.pods.values()] for k, v in self.pools.items()},
        })

    async def admin_admission(self, request: web.Request) -> web.Response:
        """GET: admission config, counters and estimate inputs. POST {"levels": "off"|"all"|"slo,fleet", "slo_s": 30, ...}."""
        if request.method == "POST":
            try:
                return web.json_response(self.admission.configure(await request.json()))
            except (ValueError, TypeError) as exc:
                return web.json_response({"error": {"message": str(exc)}}, status=400)
        return web.json_response(self.admission.state())

    async def reset_index(self, request: web.Request) -> web.Response:
        for pool in self.pools.values():
            pool.index.clear()
            for p in pool.pods.values():
                p.total = 0
        return web.json_response({"ok": True})

    async def metrics(self, request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def chat(self, request: web.Request) -> web.StreamResponse:
        t0 = time.time()
        raw = await request.read()
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return web.json_response({"error": {"message": "invalid JSON"}}, status=400)
        stream = bool(body.get("stream"))
        if stream:  # always ask for usage, so the gateway can count tokens
            opts = dict(body.get("stream_options") or {})
            opts["include_usage"] = True
            body["stream_options"] = opts
        for k, v in SAMPLING_DEFAULTS.items():
            body.setdefault(k, v)
        tenant = next((v for h in TENANT_HEADERS if (v := request.headers.get(h))), "-")
        if CACHE_SALT == "tenant":  # isolation is the gateway's call, so it overrides a client's own salt
            body["cache_salt"] = f"tenant:{tenant}"
        hashes, weights = prefix_hashes(body)
        chars = weights[-1] if weights else 0

        meta = {
            "ts": t0, "stream": stream, "tenant": tenant, "request_id": request.headers.get("x-request-id"),
            "msgs": len(body.get("messages") or []), "req_chars": chars,
        }
        ticket = Ticket(tenant=tenant, est_tokens=estimate_tokens(chars), t_enq=t0)
        meta.update(est_tokens=ticket.est_tokens, prompt_class=ticket.cls)

        # 1. admission: bucket -> fleet -> slo (each can be off)
        try:
            slo_hdr = float(request.headers["x-slo-s"]) if request.headers.get("x-slo-s") else None
        except ValueError:
            slo_hdr = None
        adm = self.admission.check(ticket, int(body.get("max_tokens") or body.get("max_completion_tokens") or 0), slo_hdr)
        meta.update(adm.info)
        if adm.shed is not None:
            REQS.labels(str(adm.shed.status)).inc()
            meta.update(status=adm.shed.status, error=adm.shed.message, e2e_s=round(time.time() - t0, 4))
            self.trace(meta)
            return adm.shed.response()

        # 3. router, run by the queue when this request's turn comes: `among` = decode pods with a free slot
        def route(among: list[Pod] | None) -> Placed:
            dec, dmatch = self.pools["decode"].pick(hashes, weights, among)
            pre, pmatch = self.pools["prefill"].pick(hashes, weights)
            placed = [("decode", dec, dmatch), ("prefill", pre, pmatch)]
            for role, p, m in placed:
                self.pools[role].remember(hashes, p.name)
                p.inflight += 1
                p.total += 1
                ROUTED.labels(role, p.name).inc()
                INFLIGHT.labels(role, p.name).set(p.inflight)
                PREFIX.labels(role).observe(m)
            return placed

        # 2. queue
        try:
            pods: Placed = await self.queue.wait(ticket, route)
        except LookupError as exc:
            self.admission.done(ticket, adm, 0, None)
            REQS.labels("503").inc()
            return web.json_response({"error": {"message": str(exc)}}, status=503)
        except asyncio.CancelledError:
            if ticket.placed:  # dispatched, but the client left before we forwarded it
                self.release(ticket.placed)
            self.admission.done(ticket, adm, 0, None)
            raise
        meta.update(queue_wait_s=round(ticket.t_out - t0, 4), queue_depth=ticket.depth)

        (_, dec, dmatch), (_, pre, pmatch) = pods
        headers = {"Content-Type": "application/json", "x-prefiller-host-port": pre.addr}
        if rid := request.headers.get("x-request-id"):
            headers["x-request-id"] = rid
        meta.update(decode_pod=dec.name, prefill_pod=pre.name, prefix_decode=round(dmatch, 3), prefix_prefill=round(pmatch, 3))
        prefill_open = True

        url = f"http://{dec.addr}/v1/chat/completions"
        first = None
        gaps: list[float] = []
        usage: dict = {}
        status = 0
        n_chunks = 0
        err = None
        resp: web.StreamResponse | None = None
        try:
            async with self.session.post(url, data=json.dumps(body).encode(), headers=headers) as r:
                status = r.status
                if not stream or r.status != 200:
                    data = await r.read()
                    if r.status == 200:
                        first = time.time()
                        try:
                            usage = json.loads(data).get("usage") or {}
                        except (json.JSONDecodeError, AttributeError):
                            pass
                    else:
                        err = data[:300].decode("utf-8", "replace")
                    return web.Response(body=data, status=r.status, content_type="application/json")
                resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
                await resp.prepare(request)
                last = None
                async for line in r.content:
                    await resp.write(line)
                    if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                        continue
                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    ch = chunk.get("choices") or []
                    delta = (ch[0].get("delta") or {}) if ch else {}
                    # thinking tokens count as output (vLLM: reasoning / reasoning_content)
                    if delta.get("content") or delta.get("tool_calls") or delta.get("reasoning_content") or delta.get("reasoning"):
                        now = time.time()
                        n_chunks += 1
                        if first is None:
                            first = now
                            if prefill_open:  # the prefill pod is done with this request
                                self.release(pods, "prefill")
                                prefill_open = False
                        elif last is not None:
                            gaps.append(now - last)
                            ITL.observe(now - last)
                        last = now
                await resp.write_eof()
                return resp
        except (OSError, asyncio.TimeoutError) as exc:
            err = repr(exc)[:300]
            status = status or 502
            if resp is None:
                return web.json_response({"error": {"message": f"upstream {dec.addr}: {err}"}}, status=502)
            return resp
        finally:
            t1 = time.time()
            if prefill_open:
                self.release(pods, "prefill")
            self.release(pods, "decode")
            REQS.labels(str(status)).inc()
            ok = status == 200 and first is not None
            if ok:
                TTFT.observe(first - t0)
                E2E.observe(t1 - t0)
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
            # settle the tenant's bucket to the tokens really used; a good call feeds the deadline estimate
            self.admission.done(ticket, adm, pt + ct, t1 - ticket.t_out if ok else None, ct)
            TOKENS.labels("prompt").inc(pt)
            TOKENS.labels("completion").inc(ct)
            TOKENS.labels("cached").inc(cached)
            gs = sorted(gaps)
            meta.update(
                status=status, error=err, ttft_s=None if first is None else round(first - t0, 4),
                e2e_s=round(t1 - t0, 4), prompt_tokens=pt, completion_tokens=ct, cached_tokens=cached,
                reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                chunks=n_chunks,
                itl_mean=round(statistics.fmean(gs), 5) if gs else None,
                itl_p50=round(gs[len(gs) // 2], 5) if gs else None,
                itl_p99=round(gs[min(len(gs) - 1, int(len(gs) * 0.99))], 5) if gs else None,
                itl_max=round(gs[-1], 5) if gs else None,
            )
            self.trace(meta)


def main() -> None:
    gw = Gateway()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(gw.start)
    app.on_cleanup.append(gw.stop)
    app.router.add_post("/v1/chat/completions", gw.chat)
    app.router.add_get("/v1/models", gw.models)
    app.router.add_get("/metrics", gw.metrics)
    app.router.add_get("/debug/pods", gw.debug_pods)
    app.router.add_post("/debug/reset", gw.reset_index)
    app.router.add_get("/admin/admission", gw.admin_admission)
    app.router.add_post("/admin/admission", gw.admin_admission)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    adm = gw.admission.state()
    print(f"pd-gateway router={POLICY} queue={QUEUE_POLICY} admission={adm['levels']} slo_s={adm['slo_s']} "
          f"cache_salt={CACHE_SALT} trace={gw.trace_path or 'off'} on :{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
