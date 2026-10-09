"""Admission: accept or turn away a request before it enters the queue.

Three levels, checked in this order; the first that says no answers (and the request never reaches
the queue or an engine):

  bucket  Token bucket per tenant (x-tenant header), refilled at BUCKET_TOKENS_PER_S up to BUCKET_BURST.
          A request takes its estimated prompt + min(max_tokens, BUCKET_OUTPUT_RESERVE) tokens; when it
          ends, the bucket is settled to the real prompt + completion tokens.
          Not enough tokens -> 429 rate_limit_error / tenant_tokens, Retry-After = time to refill.
  fleet   Every pod of a pool (prefill or decode) is unreachable or saturating (KV cache usage >=
          KV_SATURATION) -> 503 server_is_overloaded / kv_free (or no_eligible_pod), Retry-After 2.
          Pods are scraped every FLEET_SCRAPE_S; if every snapshot of a pool is older than STALE_S
          (the scraper has not run yet) the check lets the request through (fail open).
  slo     The request cannot finish before its deadline, SLO_S after arrival (or its own x-slo-s header):
            est_ttft  = queue_wait + n_in / prefill_tokens_per_s
            est_total = est_ttft + n_out * inter_token_latency
            est_total > deadline -> 429 rate_limit_error / deadline_unmeetable, Retry-After 2, with the
            estimate in the error body.
          queue_wait           = (requests ahead in the gateway queue + requests waiting in vLLM on the decode
                                 pods) / drain rate. SLO_DRAIN=littles (default): Little's law, requests in service
                                 on the decode pods / their mean service time (last SLO_WINDOW calls).
                                 SLO_DRAIN=completions: calls the gateway finished per second over the last
                                 SLO_RATE_WINDOW_S. It lags a load step (near 0 right after load jumps up, so the
                                 estimate shoots up and requests are refused); kept to reproduce earlier runs.
          n_in                 = the gateway's prompt-token estimate (gateway/queue.py)
          prefill_tokens_per_s = increase of vllm:request_prompt_tokens_sum / increase of
                                 vllm:request_prefill_time_seconds_sum on the prefill pods over the window
                                 (cached tokens count, so it is the effective rate), bounded to
                                 SLO_PREFILL_TPS_MIN..MAX; SLO_PREFILL_TPS before any measurement
          inter_token_latency  = vllm:inter_token_latency_seconds sum / count over the window on the decode pods
          n_out                = min(max_tokens, SLO_OUT_QUANTILE of the output tokens of recent calls of the
                                 same prompt class, finished in the last SLO_NOUT_WINDOW_S); nobody knows the real one
                                 in advance; SLO_NOUT_DEFAULT with fewer than SLO_MIN_SAMPLES such calls. The time
                                 limit matters: refused calls never finish, so without it a stretch of long answers
                                 could keep the estimate high (and keep refusing) long after the traffic changed.
          No call finished yet (no drain rate) -> no queue term.

ADMISSION = all (default) | off | a comma list of bucket,fleet,slo
At run time (until the next deploy): GET /admin/admission shows config, counters and the estimator inputs;
POST /admin/admission {"levels": "off" | "all" | "slo,fleet", "slo_s": 30, "bucket_tokens_per_s": ..,
"bucket_burst": .., "bucket_output_reserve": .., "kv_saturation": .., "slo_out_quantile": .., "slo_drain": ..}
or simply: bash scripts/admission.sh
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from aiohttp import ClientSession, ClientTimeout, web
from prometheus_client import Counter, Gauge, Histogram

from gateway.queue import Queue, Ticket
from gateway.router import Pod, Pool

LEVELS = ("bucket", "fleet", "slo")
STALE_S = 5.0
FLEET_SCRAPE_S = float(os.environ.get("FLEET_SCRAPE_S", "1"))
SLO_WINDOW = int(os.environ.get("SLO_WINDOW", "200"))          # recent calls kept per class for n_out
SLO_MIN_SAMPLES = int(os.environ.get("SLO_MIN_SAMPLES", "10"))
SLO_RATE_WINDOW_S = float(os.environ.get("SLO_RATE_WINDOW_S", "30"))
SLO_NOUT_WINDOW_S = float(os.environ.get("SLO_NOUT_WINDOW_S", "300"))  # 0 = no time limit
SLO_PREFILL_TPS = float(os.environ.get("SLO_PREFILL_TPS", "10000"))
SLO_PREFILL_TPS_MIN = float(os.environ.get("SLO_PREFILL_TPS_MIN", "2000"))
SLO_PREFILL_TPS_MAX = float(os.environ.get("SLO_PREFILL_TPS_MAX", "60000"))
SLO_ITL_DEFAULT = float(os.environ.get("SLO_ITL_DEFAULT", "0.02"))
SLO_NOUT_DEFAULT = int(os.environ.get("SLO_NOUT_DEFAULT", "512"))

ADMITTED = Counter("gw_admitted_total", "Requests admitted")
SHED = Counter("gw_shed_total", "Requests turned away by admission", ["level", "reason", "code"])
# every shed series exists from the start at 0: a series that first appears at 1 has no step for rate()
for _lv, _reason, _code in (("bucket", "tenant_tokens", "429"), ("fleet", "kv_free", "503"),
                            ("fleet", "no_eligible_pod", "503"), ("slo", "deadline_unmeetable", "429")):
    SHED.labels(_lv, _reason, _code)
SHED_TENANT = Counter("gw_shed_by_tenant_total", "Requests turned away, per tenant", ["tenant"])
PREDICTED = Histogram("gw_slo_predicted_seconds", "Estimated finish of a request (slo level)", ["cls"],
                      buckets=(1, 2, 5, 10, 15, 20, 26, 30, 40, 60, 90, 120))
BUCKET_LEVEL = Gauge("gw_bucket_tokens", "Tokens left in a tenant's bucket", ["tenant"])
FLEET_KV = Gauge("gw_fleet_kv_usage", "KV cache usage per pod, as admission sees it", ["role", "pod"])
FLEET_OK = Gauge("gw_fleet_eligible", "1 = pod eligible for admission (fresh, healthy, not saturating)", ["role", "pod"])
LEVEL_ON = Gauge("gw_admission_level", "1 = this admission level is on", ["level"])
SLO_G = Gauge("gw_admission_slo_seconds", "Deadline the slo level checks against (SLO_S)")
KV_SAT_G = Gauge("gw_admission_kv_saturation", "KV usage at which the fleet level calls a pod saturating")
SLO_INPUT = Gauge("gw_slo_input", "Inputs of the deadline estimate: prefill_tokens_per_s, inter_token_latency_s, "
                  "drain_per_s, vllm_waiting, n_out_short, n_out_long", ["name"])


def parse_levels(spec: str) -> set[str]:
    spec = (spec or "off").strip().lower()
    if spec in ("off", "none", "0", "false", ""):
        return set()
    if spec in ("all", "on", "1", "true"):
        return set(LEVELS)
    levels = {s.strip() for s in spec.split(",") if s.strip()}
    if bad := levels - set(LEVELS):
        raise ValueError(f"unknown admission level(s) {sorted(bad)}; use off, all or a list of {', '.join(LEVELS)}")
    return levels


@dataclass
class Shed:
    """Why a request is turned away, as an OpenAI-style error."""
    status: int
    error: str
    reason: str
    message: str
    level: str
    retry_after_s: float | None = None
    extra: dict = field(default_factory=dict)  # e.g. the deadline estimate, in the error body

    def response(self) -> web.Response:
        headers = {}
        if self.status in (429, 503):
            headers["Retry-After"] = str(max(1, round(self.retry_after_s or 2)))
        return web.json_response({"error": {"type": self.error, "code": self.reason, "message": self.message, **self.extra}},
                                 status=self.status, headers=headers)


@dataclass
class Decision:
    shed: Shed | None = None
    reserved: int = 0  # tokens taken from the tenant's bucket, settled when the request ends
    info: dict = field(default_factory=dict)  # goes into the gateway trace line


class TokenBuckets:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self.level: dict[str, float] = {}
        self.t: dict[str, float] = {}

    def _refill(self, tenant: str, now: float) -> float:
        lvl = self.level.get(tenant, self.burst)
        lvl = min(self.burst, lvl + (now - self.t.get(tenant, now)) * self.rate)
        self.level[tenant], self.t[tenant] = lvl, now
        return lvl

    def take(self, tenant: str, cost: float, now: float) -> float | None:
        """None = taken; else seconds until the bucket holds enough. A request bigger than the burst needs a full bucket."""
        need = min(cost, self.burst)
        lvl = self._refill(tenant, now)
        if lvl < need:
            return (need - lvl) / self.rate if self.rate > 0 else 60.0
        self.level[tenant] = lvl - cost  # may go below zero for a request bigger than the burst: a debt to refill
        BUCKET_LEVEL.labels(tenant).set(self.level[tenant])
        return None

    def give(self, tenant: str, tokens: float, now: float) -> None:
        """Settle: + refund (reserved more than used) or - debt (used more than reserved)."""
        lvl = self._refill(tenant, now)
        self.level[tenant] = min(self.burst, lvl + tokens)
        BUCKET_LEVEL.labels(tenant).set(self.level[tenant])


@dataclass
class PodState:
    kv: float | None = None
    waiting: float | None = None
    running: float | None = None
    t: float = 0.0  # last scrape
    healthy: bool = True
    hist: deque = field(default_factory=lambda: deque(maxlen=240))  # (t, prompt_sum, prefill_s, itl_sum, itl_n)


def parse_engine_metrics(text: str) -> dict[str, float]:
    """The few vLLM /metrics values admission needs (first sample of each)."""
    names = {"vllm:kv_cache_usage_perc": "kv", "vllm:gpu_cache_usage_perc": "kv",
             "vllm:num_requests_waiting": "waiting", "vllm:num_requests_running": "running",
             # cumulative sums -> rates over SLO_RATE_WINDOW_S
             "vllm:request_prompt_tokens_sum": "prompt_sum", "vllm:request_prefill_time_seconds_sum": "prefill_s",
             "vllm:inter_token_latency_seconds_sum": "itl_sum", "vllm:inter_token_latency_seconds_count": "itl_n"}
    out: dict[str, float] = {}
    for raw in text.splitlines():
        if not raw or raw[0] == "#":
            continue
        key = names.get(raw.split("{", 1)[0].split(" ", 1)[0])
        if key is None or key in out:
            continue
        try:
            out[key] = float(raw.rsplit(None, 1)[-1])
        except ValueError:
            continue
    if out.get("kv", 0) > 1.0:  # some exporters report percent
        out["kv"] /= 100.0
    return out


class Fleet:
    """Scrapes every pod's engine /metrics and decides which pods are eligible."""

    def __init__(self, pools: dict[str, Pool], metrics_url: Callable[[str, Pod], str], kv_saturation: float) -> None:
        self.pools, self.metrics_url, self.kv_saturation = pools, metrics_url, kv_saturation
        self.state: dict[tuple[str, str], PodState] = {}

    async def scrape(self, session: ClientSession) -> None:
        async def one(role: str, pod: Pod) -> None:
            st = self.state.setdefault((role, pod.name), PodState())
            try:
                async with session.get(self.metrics_url(role, pod), timeout=ClientTimeout(total=2)) as r:
                    m = parse_engine_metrics(await r.text()) if r.status == 200 else None
            except (OSError, asyncio.TimeoutError):
                m = None
            # an unreachable pod is a fresh, unhealthy snapshot; "stale" only means the scraper has not run lately
            st.healthy, st.t = m is not None, time.time()
            if m is not None:
                st.kv, st.waiting, st.running = m.get("kv"), m.get("waiting"), m.get("running")
                st.hist.append((st.t, m.get("prompt_sum"), m.get("prefill_s"), m.get("itl_sum"), m.get("itl_n")))
                if st.kv is not None:
                    FLEET_KV.labels(role, pod.name).set(st.kv)
        await asyncio.gather(*(one(role, p) for role, pool in self.pools.items() for p in list(pool.pods.values())))

    def check(self, now: float) -> Shed | None:
        for role, pool in self.pools.items():
            eligible, stale, saturating = [], 0, 0
            for p in pool.pods.values():
                st = self.state.get((role, p.name))
                fresh = st is not None and st.t and now - st.t <= STALE_S
                sat = bool(fresh and st.kv is not None and st.kv >= self.kv_saturation)
                ok = bool(fresh and st.healthy and not sat)
                FLEET_OK.labels(role, p.name).set(1 if ok else 0)
                stale += not fresh
                saturating += sat
                if ok:
                    eligible.append(p)
            if eligible or not pool.pods or stale == len(pool.pods):  # every snapshot stale: fail open
                continue
            reason = "kv_free" if saturating else "no_eligible_pod"
            return Shed(503, "server_is_overloaded", reason, f"fleet saturating: no eligible {role} pod", "fleet", 2)
        return None

    def rates(self, window_s: float) -> tuple[float | None, float | None, float]:
        """(prefill tokens/s on the prefill pods, inter-token latency s and requests waiting in vLLM on the
        decode pods), from the scraped engine counters."""
        now = time.time()
        tok = pre_s = itl_s = itl_n = waiting = 0.0
        for role in ("prefill", "decode"):
            for p in self.pools[role].pods.values():
                st = self.state.get((role, p.name))
                if st is None:
                    continue
                h = [x for x in st.hist if now - x[0] <= window_s]
                if len(h) >= 2:
                    a, b = h[0], h[-1]
                    if role == "prefill" and None not in (a[1], b[1], a[2], b[2]) and b[2] >= a[2] and b[1] >= a[1]:
                        tok += b[1] - a[1]
                        pre_s += b[2] - a[2]
                    if role == "decode" and None not in (a[3], b[3], a[4], b[4]) and b[4] >= a[4] and b[3] >= a[3]:
                        itl_s += b[3] - a[3]
                        itl_n += b[4] - a[4]
                if role == "decode" and st.waiting is not None and st.t and now - st.t <= STALE_S:
                    waiting += st.waiting
        return (tok / pre_s if pre_s > 0 else None), (itl_s / itl_n if itl_n > 0 else None), waiting


class Recent:
    """Recent finished calls: when they ended, how long they ran (the drain rate) and how many tokens they
    wrote, per prompt class (n_out)."""

    def __init__(self, window: int) -> None:
        self.out_by_cls: dict[str, deque[tuple[float, int]]] = {"short": deque(maxlen=window), "long": deque(maxlen=window)}
        self.runs: deque[float] = deque(maxlen=window)  # service time: dispatch -> last token
        self.ends: deque[float] = deque(maxlen=4096)

    def add(self, cls: str, out_tokens: int, run_s: float) -> None:
        now = time.time()
        self.out_by_cls.setdefault(cls, deque(maxlen=SLO_WINDOW)).append((now, out_tokens))
        self.runs.append(run_s)
        self.ends.append(now)

    def completions(self, window_s: float) -> float | None:
        """Calls finished per second over the last window_s (None before any call finished)."""
        now = time.time()
        n = sum(1 for t in self.ends if now - t <= window_s)
        return n / window_s if self.ends else None

    def littles(self, in_service: int) -> float | None:
        """Little's law: departures per second = requests in service / mean service time (None before any call finished)."""
        if not self.runs or in_service <= 0:
            return None
        return in_service / max(statistics.fmean(self.runs), 0.1)

    def n_out(self, cls: str, q: float) -> float | None:
        cut = time.time() - SLO_NOUT_WINDOW_S if SLO_NOUT_WINDOW_S > 0 else 0.0
        own = sorted(n for t, n in self.out_by_cls.get(cls) or () if t >= cut)
        if len(own) < SLO_MIN_SAMPLES:  # too few of this class: use every class
            own = sorted(n for d in self.out_by_cls.values() for t, n in d if t >= cut)
        return own[min(len(own) - 1, int(q * len(own)))] if len(own) >= SLO_MIN_SAMPLES else None


class Admission:
    def __init__(self, pools: dict[str, Pool], queue: Queue, metrics_url: Callable[[str, Pod], str]) -> None:
        self.queue = queue
        self.levels = parse_levels(os.environ.get("ADMISSION", "all"))
        self.slo_s = float(os.environ.get("SLO_S", "26"))
        self.output_reserve = int(os.environ.get("BUCKET_OUTPUT_RESERVE", "1024"))
        self.buckets = TokenBuckets(float(os.environ.get("BUCKET_TOKENS_PER_S", "3000")),
                                    float(os.environ.get("BUCKET_BURST", "60000")))
        self.fleet = Fleet(pools, metrics_url, float(os.environ.get("KV_SATURATION", "0.80")))
        self.recent = Recent(SLO_WINDOW)
        self.out_q = float(os.environ.get("SLO_OUT_QUANTILE", "0.5"))
        self.drain_mode = os.environ.get("SLO_DRAIN", "littles")  # littles | completions
        if self.drain_mode not in ("littles", "completions"):
            raise SystemExit(f"unknown SLO_DRAIN {self.drain_mode!r} (littles | completions)")
        self.inputs: dict = {}
        self.counts: dict[str, int] = {"admitted": 0}
        self._gauges()

    def _gauges(self) -> None:
        for lv in LEVELS:
            LEVEL_ON.labels(lv).set(1 if lv in self.levels else 0)
        SLO_G.set(self.slo_s)
        KV_SAT_G.set(self.fleet.kv_saturation)

    async def fleet_loop(self, session: ClientSession) -> None:
        while True:
            if self.levels & {"fleet", "slo"}:
                try:
                    await self.fleet.scrape(session)
                except Exception as exc:  # noqa: BLE001
                    print(f"fleet scrape error: {exc!r}", flush=True)
                if "slo" in self.levels:
                    self.refresh_inputs()
            await asyncio.sleep(FLEET_SCRAPE_S)

    def refresh_inputs(self) -> dict:
        """The estimate's inputs, read from the engines and from the gateway's own finished calls."""
        tps, itl, waiting = self.fleet.rates(SLO_RATE_WINDOW_S)
        tps = SLO_PREFILL_TPS if tps is None else min(max(tps, SLO_PREFILL_TPS_MIN), SLO_PREFILL_TPS_MAX)  # bound it, don't trust it
        if self.drain_mode == "littles":
            drain = self.recent.littles(sum(p.inflight for p in self.fleet.pools["decode"].pods.values()))
        else:
            drain = self.recent.completions(SLO_RATE_WINDOW_S)
        self.inputs = {"prefill_tokens_per_s": round(tps), "inter_token_latency_s": round(itl if itl is not None else SLO_ITL_DEFAULT, 4),
                       "itl_measured": itl is not None, "vllm_waiting": waiting, "drain": self.drain_mode,
                       "drain_per_s": None if drain is None else round(drain, 3),
                       "n_out": {c: self.recent.n_out(c, self.out_q) for c in ("short", "long")}}
        SLO_INPUT.labels("prefill_tokens_per_s").set(self.inputs["prefill_tokens_per_s"])
        SLO_INPUT.labels("inter_token_latency_s").set(self.inputs["inter_token_latency_s"])
        SLO_INPUT.labels("vllm_waiting").set(waiting)
        SLO_INPUT.labels("drain_per_s").set(self.inputs["drain_per_s"] or 0)
        for c in ("short", "long"):
            SLO_INPUT.labels(f"n_out_{c}").set(self.inputs["n_out"][c] or SLO_NOUT_DEFAULT)
        return self.inputs

    def estimate(self, t: Ticket, max_tokens: int) -> dict:
        """est_ttft = queue_wait + n_in / prefill_tokens_per_s; est_total = est_ttft + n_out * ITL."""
        # Little's law reads the in-flight count, which changes with every request: refresh per estimate
        inp = self.refresh_inputs() if self.drain_mode == "littles" or not self.inputs else self.inputs
        waiting, free, _ = self.queue.load()
        ahead = (max(0, waiting + 1 - free) if self.queue.policy is not None else 0) + inp["vllm_waiting"]
        drain = inp["drain_per_s"]
        queue_wait = 0.0 if not ahead else (ahead / drain if drain else 0.0)  # nothing finished yet: no basis, admit
        n_out = inp["n_out"][t.cls] or SLO_NOUT_DEFAULT
        if max_tokens:
            n_out = min(n_out, max_tokens)
        est_ttft = queue_wait + t.est_tokens / inp["prefill_tokens_per_s"]
        est_total = est_ttft + n_out * inp["inter_token_latency_s"]
        return {"est_total": round(est_total, 2), "est_ttft": round(est_ttft, 2), "queue_wait": round(queue_wait, 2), "ahead": ahead,
                "n_in": t.est_tokens, "n_out": round(n_out), "prefill_tokens_per_s": inp["prefill_tokens_per_s"],
                "inter_token_latency_s": inp["inter_token_latency_s"], "drain_per_s": None if drain is None else round(drain, 3)}

    def check(self, t: Ticket, max_tokens: int, slo_s: float | None = None) -> Decision:
        d = Decision(info={"admission": ",".join(sorted(self.levels)) or "off"})
        if not self.levels:  # admission off: everything is admitted (and counted, so Grafana shows the rate)
            self.counts["admitted"] += 1
            ADMITTED.inc()
            return d
        now = time.time()
        if "bucket" in self.levels:
            cost = t.est_tokens + min(max_tokens or self.output_reserve, self.output_reserve)
            wait = self.buckets.take(t.tenant, cost, now)
            if wait is not None:
                return self._shed(d, t, Shed(429, "rate_limit_error", "tenant_tokens",
                                             f"tenant {t.tenant}: token bucket empty", "bucket", wait))
            d.reserved = cost
        if "fleet" in self.levels and (s := self.fleet.check(now)):
            return self._shed(d, t, s, now)
        if "slo" in self.levels:
            slo = slo_s or self.slo_s
            e = self.estimate(t, max_tokens)
            d.info.update(slo_s=slo, est=e)
            PREDICTED.labels(t.cls).observe(e["est_total"])
            if e["est_total"] > slo:
                return self._shed(d, t, Shed(429, "rate_limit_error", "deadline_unmeetable",
                                             f"estimated completion {e['est_total']:.1f} s > deadline {slo:g} s", "slo", 2,
                                             extra={"est": e, "deadline_s": slo}), now)
        self.counts["admitted"] += 1
        ADMITTED.inc()
        return d

    def _shed(self, d: Decision, t: Ticket, s: Shed, now: float | None = None) -> Decision:
        if d.reserved:  # turned away after the bucket took its share: give it back
            self.buckets.give(t.tenant, d.reserved, now or time.time())
            d.reserved = 0
        d.shed = s
        d.info.update(shed=s.reason, shed_level=s.level)
        key = f"{s.level}/{s.reason}"
        self.counts[key] = self.counts.get(key, 0) + 1
        SHED.labels(s.level, s.reason, str(s.status)).inc()
        SHED_TENANT.labels(t.tenant).inc()
        return d

    def done(self, t: Ticket, d: Decision, used_tokens: int, run_s: float | None, out_tokens: int = 0) -> None:
        """Request ended: settle the bucket to the tokens really used. A good call (run_s = its service time,
        dispatch to last token) feeds the drain rate and n_out."""
        if d.reserved:
            self.buckets.give(t.tenant, d.reserved - used_tokens, time.time())
        if run_s is not None:
            self.recent.add(t.cls, out_tokens, run_s)

    def state(self) -> dict:
        return {"levels": sorted(self.levels) or "off", "slo_s": self.slo_s,
                "bucket": {"tokens_per_s": self.buckets.rate, "burst": self.buckets.burst, "output_reserve": self.output_reserve,
                           "tenants": {k: round(v) for k, v in self.buckets.level.items()}},
                "fleet": {"kv_saturation": self.fleet.kv_saturation,
                          "pods": {f"{r}/{n}": {"kv": s.kv, "waiting": s.waiting, "running": s.running, "healthy": s.healthy,
                                                "age_s": round(time.time() - s.t, 1) if s.t else None}
                                   for (r, n), s in self.fleet.state.items()}},
                "slo": {"drain": self.drain_mode, "out_quantile": self.out_q, "nout_window_s": SLO_NOUT_WINDOW_S, "inputs": self.inputs},
                "counts": self.counts}

    def configure(self, cfg: dict) -> dict:
        if "levels" in cfg:
            self.levels = parse_levels(str(cfg["levels"]))
        if "slo_s" in cfg:
            self.slo_s = float(cfg["slo_s"])
        if "bucket_tokens_per_s" in cfg:
            self.buckets.rate = float(cfg["bucket_tokens_per_s"])
        if "bucket_burst" in cfg:
            self.buckets.burst = float(cfg["bucket_burst"])
        if "bucket_output_reserve" in cfg:
            self.output_reserve = int(cfg["bucket_output_reserve"])
        if "kv_saturation" in cfg:
            self.fleet.kv_saturation = float(cfg["kv_saturation"])
        if "slo_drain" in cfg:
            if cfg["slo_drain"] not in ("littles", "completions"):
                raise ValueError("slo_drain: littles | completions")
            self.drain_mode = cfg["slo_drain"]
        if "slo_out_quantile" in cfg:
            q = float(cfg["slo_out_quantile"])
            if not 0 <= q <= 1:
                raise ValueError("slo_out_quantile: 0..1")
            self.out_q = q
        self._gauges()
        print(f"admission now {self.state()['levels']} slo_s={self.slo_s} drain={self.drain_mode}", flush=True)
        return self.state()
