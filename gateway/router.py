"""Router: which decode pod and which prefill pod serve a request.

The gateway asks Pool.pick once per request, when the queue lets the request go. The decode pod is
picked only among decode pods with a free slot (late binding); the prefill pod among all prefill pods.

ROUTER_POLICY
  prefix-load   (default) prefix affinity with bounded load: a pod is eligible while its in-flight
                count stays under ceil(mean * (1 + LOAD_SLACK)); among eligible pods the one that has
                seen the longest prefix of this prompt wins, ties go to the least loaded
  least-loaded  the pod with the fewest requests in flight (random among ties)
  round-robin   strict rotation over the pods (sorted by name), blind to load and prefix
  random
The prefix match is the share of the prompt (in characters) whose message-level prefix hash was
already sent to that pod: a cheap stand-in for "this pod has the prompt's KV cached" that needs no
tokenizer.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import socket
from collections import OrderedDict
from dataclasses import dataclass, field

LOAD_SLACK = float(os.environ.get("LOAD_SLACK", "0.25"))
POLICY = os.environ.get("ROUTER_POLICY", "prefix-load")
if POLICY not in ("prefix-load", "least-loaded", "round-robin", "random"):
    raise SystemExit(f"unknown ROUTER_POLICY {POLICY!r} (prefix-load | least-loaded | round-robin | random)")
INDEX_CAP = int(os.environ.get("PREFIX_INDEX_CAP", "200000"))


@dataclass
class Pod:
    ip: str
    port: int
    # id in routing, traces and metrics: the IP, or ip:port when the pods of a pool share an IP
    # (host-network prefill: PREFILL_DNS = "vllm-prefill-0-pods:18100,vllm-prefill-1-pods:18101")
    name: str = ""
    inflight: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        self.name = self.name or self.ip

    @property
    def addr(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass
class Pool:
    role: str
    targets: list[tuple[str, int]]  # (dns name, port), usually one headless service
    pods: dict[str, Pod] = field(default_factory=dict)
    # prefix hash -> pods that served a prompt with this prefix (so probably have its KV cached)
    index: OrderedDict[str, set[str]] = field(default_factory=OrderedDict)
    rr: int = 0  # next position for ROUTER_POLICY=round-robin

    @classmethod
    def from_spec(cls, role: str, spec: str) -> Pool:
        """spec = comma list of dns:port, e.g. "vllm-decode-pods:8000"."""
        targets = []
        for t in spec.split(","):
            host, _, port = t.strip().rpartition(":")
            targets.append((host, int(port)))
        return cls(role=role, targets=targets)

    async def discover(self) -> None:
        """Resolve the DNS targets (headless services list Ready pods only) into pods."""
        loop = asyncio.get_running_loop()
        found: dict[str, tuple[str, int]] = {}
        for host, port in self.targets:
            try:
                infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            except OSError:  # DNS hiccup or nothing Ready yet: keep this target's last known pods
                found.update({n: (p.ip, p.port) for n, p in self.pods.items() if p.port == port})
                continue
            for ip in {i[4][0] for i in infos}:
                found[ip if len(self.targets) == 1 else f"{ip}:{port}"] = (ip, port)
        if not found:
            return
        before = set(self.pods)
        for name, (ip, port) in found.items():
            self.pods.setdefault(name, Pod(ip, port, name))
        for gone in before - set(found):
            self.pods.pop(gone, None)
        if before != set(found):
            print(f"discovery {self.role}: {', '.join(sorted(found))}", flush=True)

    def remember(self, hashes: list[str], name: str) -> None:
        for h in hashes:
            self.index.setdefault(h, set()).add(name)
            self.index.move_to_end(h)
        while len(self.index) > INDEX_CAP:
            self.index.popitem(last=False)

    def match(self, hashes: list[str], weights: list[int]) -> dict[str, float]:
        """Share of the prompt each pod has seen as a prefix (longest match wins)."""
        total = weights[-1] if weights else 1
        out: dict[str, float] = {}
        for h, w in zip(reversed(hashes), reversed(weights)):
            for name in self.index.get(h, ()):
                if name in self.pods and name not in out:
                    out[name] = w / total
        return out

    def pick(self, hashes: list[str], weights: list[int], among: list[Pod] | None = None) -> tuple[Pod, float]:
        """Prefix affinity with bounded load (consistent hashing with bounded loads, Mirrokni et al. 2018).

        Without the bound every request would stick to whichever pod first cached a shared system
        prompt (in a pilot run one prefill pod took 63% of all requests).
        `among` = the pods the queue allows (a free slot); None = every pod."""
        pods = list(self.pods.values())
        if not pods:
            raise LookupError(f"no ready {self.role} pod behind {self.targets}")
        cands = pods if among is None else among
        matches = self.match(hashes, weights)
        if POLICY == "round-robin":
            ring = sorted(cands, key=lambda p: p.name)
            p = ring[self.rr % len(ring)]
            self.rr += 1
            return p, matches.get(p.name, 0.0)
        if POLICY == "random":
            p = random.choice(cands)
            return p, matches.get(p.name, 0.0)
        if POLICY == "least-loaded":
            low = min(p.inflight for p in cands)
            p = random.choice([p for p in cands if p.inflight == low])
            return p, matches.get(p.name, 0.0)
        cap = math.ceil((sum(p.inflight for p in pods) + 1) / len(pods) * (1 + LOAD_SLACK))
        eligible = [p for p in cands if p.inflight + 1 <= cap] or cands
        key = lambda p: (round(matches.get(p.name, 0.0), 6), -p.inflight)  # noqa: E731
        top = max(key(p) for p in eligible)
        p = random.choice([p for p in eligible if key(p) == top])
        return p, matches.get(p.name, 0.0)


def prefix_hashes(body: dict) -> tuple[list[str], list[int]]:
    """Cumulative hash per message boundary: (salt + tools), +msg1, +msg2, ... and the prompt size so far.

    Messages are serialised with sorted keys, so the hash is stable across the turns of a conversation
    (clients re-send the history byte for byte). A cache_salt splits vLLM's prefix cache, so it seeds
    the hash too: the router sees the same split as the engines."""
    h = hashlib.sha1()
    if salt := body.get("cache_salt"):
        h.update(f"salt:{salt}\n".encode())
    h.update(json.dumps(body.get("tools") or [], sort_keys=True).encode())
    hashes, weights, size = [], [], 0
    for m in body.get("messages") or []:
        blob = json.dumps(m, sort_keys=True, ensure_ascii=False).encode()
        h.update(blob)
        size += len(blob)
        hashes.append(h.copy().hexdigest())
        weights.append(size)
    return hashes, weights
