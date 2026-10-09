"""Queue: hold a request until a decode pod has a free slot; QUEUE_POLICY decides who goes next.

A slot = one request in flight on a decode pod, from dispatch to the end of its stream. Each decode
pod has QUEUE_SLOTS slots (deploy sets it to vLLM's --max-num-seqs), so the engines' own waiting
queues stay short and the order is decided here. The router then picks among the decode pods with
a free slot.

QUEUE_POLICY
  two-class  (default) short prompts (estimated <= LONG_PROMPT_TOKENS) and long prompts wait in two
             queues, served 9:1 (QUEUE_CLASS_WEIGHTS) by smooth weighted round-robin over the classes
             that have work: while both wait, 9 of every 10 dispatches are short; when one class is
             empty the other gets every slot. Inside each class, tenants take turns (tenant-rr).
  fcfs       one FIFO
  tenant-rr  one FIFO per tenant (x-tenant header), served round-robin over the tenants that have work
  none       no queue: every request is sent at once and waits in vLLM's own FCFS queue

The gateway has no tokenizer, so prompt tokens are estimated from the size of the messages:
TOKENS_PER_CHAR * chars + TOKENS_BASE (a least-squares fit over 1,233 real agent calls: 2.2% median
error). Fit it again for your own traffic: compare est_tokens with prompt_tokens in the trace lines.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from prometheus_client import Gauge, Histogram

from gateway.router import Pod, Pool

QUEUE_POLICY = os.environ.get("QUEUE_POLICY", "two-class")
if QUEUE_POLICY not in ("none", "fcfs", "tenant-rr", "two-class"):
    raise SystemExit(f"unknown QUEUE_POLICY {QUEUE_POLICY!r} (two-class | fcfs | tenant-rr | none)")
SLOTS = int(os.environ.get("QUEUE_SLOTS", "9"))
LONG_PROMPT_TOKENS = int(os.environ.get("LONG_PROMPT_TOKENS", "10000"))
CLASS_WEIGHTS = dict(zip(("short", "long"), (int(w) for w in os.environ.get("QUEUE_CLASS_WEIGHTS", "9:1").split(":"))))
TOKENS_PER_CHAR = float(os.environ.get("TOKENS_PER_CHAR", "0.3371"))
TOKENS_BASE = float(os.environ.get("TOKENS_BASE", "729"))

WAIT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30, 45, 60, 90, 120)
DEPTH = Gauge("gw_queue_depth", "Requests waiting in the gateway queue", ["policy", "cls"])
WAIT = Histogram("gw_queue_wait_seconds", "Time a request waited in the gateway queue", ["policy", "cls"],
                 buckets=WAIT_BUCKETS)


def estimate_tokens(chars: int) -> int:
    return round(TOKENS_PER_CHAR * chars + TOKENS_BASE)


@dataclass(eq=False)  # identity equality: deque.remove must find this very ticket
class Ticket:
    tenant: str
    est_tokens: int
    t_enq: float
    cls: str = ""  # short | long
    depth: int = 0  # requests already waiting when this one arrived
    t_out: float | None = None  # dispatched
    placed: Any = None  # what route() returned: the pods it was counted on
    route: Callable[[list[Pod] | None], Any] | None = None
    fut: asyncio.Future | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.cls = self.cls or ("long" if self.est_tokens > LONG_PROMPT_TOKENS else "short")


class FCFS:
    def __init__(self) -> None:
        self.q: deque[Ticket] = deque()

    def __len__(self) -> int:
        return len(self.q)

    def push(self, t: Ticket) -> None:
        self.q.append(t)

    def pop(self) -> Ticket:
        return self.q.popleft()

    def remove(self, t: Ticket) -> None:
        self.q.remove(t)


class TenantRR:
    """One FIFO per tenant; the tenants with work take turns (a tenant that runs dry leaves the
    rotation and rejoins at the back)."""

    def __init__(self) -> None:
        self.q: OrderedDict[str, deque[Ticket]] = OrderedDict()
        self.n = 0

    def __len__(self) -> int:
        return self.n

    def push(self, t: Ticket) -> None:
        self.q.setdefault(t.tenant, deque()).append(t)
        self.n += 1

    def pop(self) -> Ticket:
        tenant, dq = next(iter(self.q.items()))
        t = dq.popleft()
        if dq:
            self.q.move_to_end(tenant)
        else:
            del self.q[tenant]
        self.n -= 1
        return t

    def remove(self, t: Ticket) -> None:
        dq = self.q[t.tenant]
        dq.remove(t)
        if not dq:
            del self.q[t.tenant]
        self.n -= 1


class TwoClass:
    """Short / long prompt classes, smooth weighted round-robin (as in nginx) over the classes that
    have work: each pop adds every waiting class's weight to its credit, the highest credit goes and
    pays the sum of the waiting classes' weights. 9:1 gives S S S S S L S S S S while both wait."""

    def __init__(self, weights: dict[str, int]) -> None:
        self.w = weights
        self.cls = {c: TenantRR() for c in weights}
        self.credit = {c: 0 for c in weights}

    def __len__(self) -> int:
        return sum(len(q) for q in self.cls.values())

    def push(self, t: Ticket) -> None:
        self.cls[t.cls].push(t)

    def pop(self) -> Ticket:
        active = [c for c, q in self.cls.items() if len(q)]
        for c in self.cls:
            if c not in active:
                self.credit[c] = 0
        for c in active:
            self.credit[c] += self.w[c]
        best = max(active, key=lambda c: self.credit[c])
        self.credit[best] -= sum(self.w[c] for c in active)
        return self.cls[best].pop()

    def remove(self, t: Ticket) -> None:
        self.cls[t.cls].remove(t)


class Queue:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool  # the slots live on the decode pods
        self.policy = {"none": None, "fcfs": FCFS, "tenant-rr": TenantRR,
                       "two-class": lambda: TwoClass(CLASS_WEIGHTS)}[QUEUE_POLICY]
        self.policy = self.policy() if self.policy else None
        self.waiting: Counter[str] = Counter()
        for c in CLASS_WEIGHTS:
            DEPTH.labels(QUEUE_POLICY, c).set(0)

    def state(self) -> dict:
        return {"policy": QUEUE_POLICY, "slots_per_pod": SLOTS, "long_prompt_tokens": LONG_PROMPT_TOKENS,
                "class_weights": CLASS_WEIGHTS, "waiting": dict(self.waiting)}

    def load(self) -> tuple[int, int, int]:
        """(requests waiting here, free slots, all slots), for admission's deadline estimate."""
        pods = list(self.pool.pods.values())
        return (len(self.policy) if self.policy else 0, sum(max(0, SLOTS - p.inflight) for p in pods), SLOTS * len(pods))

    async def wait(self, t: Ticket, route: Callable[[list[Pod] | None], Any]) -> Any:
        """Returns route(pods with a free slot) once this request's turn comes; route counts it in flight."""
        if not self.pool.pods:
            raise LookupError(f"no ready {self.pool.role} pod behind {self.pool.targets}")
        t.route = route
        if self.policy is None:
            t.placed, t.t_out = route(None), time.time()
            return t.placed
        t.depth = len(self.policy)
        t.fut = asyncio.get_running_loop().create_future()
        self._count(t, +1)
        self.policy.push(t)
        self.kick()
        try:
            return await t.fut
        except asyncio.CancelledError:  # the client left while waiting
            if t.t_out is None:
                self.policy.remove(t)
                self._count(t, -1)
            raise

    def kick(self) -> None:
        """Dispatch waiting requests while some decode pod has a free slot. Called when a request
        arrives, when one leaves its slot (end of stream) and after pod discovery."""
        if self.policy is None:
            return
        while len(self.policy):
            free = [p for p in self.pool.pods.values() if p.inflight < SLOTS]
            if not free:
                return
            t = self.policy.pop()
            self._count(t, -1)
            t.t_out = time.time()
            if t.fut.done():  # cancelled a moment ago; its handler cleans up
                continue
            WAIT.labels(QUEUE_POLICY, t.cls).observe(t.t_out - t.t_enq)
            try:
                t.placed = t.route(free)
            except LookupError as exc:
                t.fut.set_exception(exc)
                continue
            t.fut.set_result(t.placed)

    def _count(self, t: Ticket, d: int) -> None:
        self.waiting[t.cls] += d
        DEPTH.labels(QUEUE_POLICY, t.cls).set(self.waiting[t.cls])
