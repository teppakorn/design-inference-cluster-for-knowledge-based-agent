#!/usr/bin/env python3
"""Load test for the gateway: N simulated users chat with the model at the same time.

Each user belongs to a tenant and runs multi-turn conversations: the first message carries a
synthetic report (made up on the fly, so there is no data to download), the next turns ask
follow-up questions and the history grows, like a real chat or agent session. A share of the
conversations (--long-share, spread evenly) use a long report, so the two-class queue sees long prompts.
A refused call (429 / 503) is retried after its Retry-After, up to --retries times; after that the
user gives up on the conversation and starts a new one.

Thinking is off by default: answers are then a few hundred tokens, a call takes a few seconds, and the
default 26 s SLO fits. With --thinking the model may think up to --max-tokens; at ~18 ms per token,
2,048 tokens take ~37 s, so raise the SLO first (bash scripts/admission.sh slo-s 90) or admission will
refuse most calls, correctly: they cannot finish in 26 s.

    python3 loadtest/loadtest.py                                  # 30 users for 3 minutes
    python3 loadtest/loadtest.py --users 10 --duration 60 --long-share 0
    python3 loadtest/loadtest.py --retries 0 --out traces/run1.jsonl
    python3 loadtest/loadtest.py --thinking --max-tokens 2048       # after: bash scripts/admission.sh slo-s 90

Standard library only. Prints a summary at the end; --out writes one JSON line per call.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

SUBJECTS = ["Revenue", "Operating cost", "Customer churn", "Average order value", "Delivery time", "Support tickets",
            "Gross margin", "Inventory turnover", "Active users", "Return rate", "Marketing spend", "Cloud cost",
            "Headcount", "Conversion rate", "Net promoter score", "Warehouse utilisation"]
REGIONS = ["the north region", "the south region", "the east region", "the west region", "the central hub",
           "the online store", "the partner channel", "the enterprise segment", "small businesses", "new customers"]
MOVES = ["rose", "fell", "stayed flat", "jumped", "slipped", "recovered", "doubled", "dropped sharply"]
CAUSES = ["a pricing change", "a new supplier", "seasonal demand", "a delayed product launch", "a system outage",
          "a marketing campaign", "higher fuel prices", "a staff shortage", "a new loyalty programme", "a currency swing",
          "a software migration", "a competitor's discount", "better forecasting", "a warehouse move"]
TITLES = ["Sales", "Costs", "Customers", "Operations", "Logistics", "Product", "People", "Technology", "Risks", "Outlook"]
QUESTIONS = ["Summarise section {a} in three bullet points.",
             "Which numbers in section {a} look unusual, and why might that be?",
             "Compare section {a} with section {b}. What changed?",
             "Write a short email to the team about the biggest risk in this report.",
             "List three questions a manager should ask after reading section {a}.",
             "Explain the main cause behind the changes in section {b} in plain words.",
             "Which region needs the most attention? Answer in two sentences.",
             "Turn section {a} into a table with columns metric, change, cause."]
SYSTEM = ("You are an analyst assistant. You read internal business reports and answer questions about them. "
          "Be precise, quote numbers from the report, and keep answers short unless asked otherwise.")


def make_report(rng: random.Random, words: int) -> tuple[str, int]:
    """A made-up business report of about `words` words, in numbered sections. Returns (text, sections)."""
    out, n, sec = [], 0, 0
    while n < words:
        sec += 1
        title = f"## Section {sec}: {rng.choice(TITLES)}"
        out.append(title)
        n += len(title.split())
        for _ in range(rng.randint(6, 12)):
            s = (f"{rng.choice(SUBJECTS)} in {rng.choice(REGIONS)} {rng.choice(MOVES)} by {rng.uniform(0.5, 40):.1f}% "
                 f"to {rng.randint(100, 99999):,} in week {rng.randint(1, 52)}, mainly because of {rng.choice(CAUSES)}.")
            out.append(s)
            n += len(s.split())
    return "\n".join(out), sec


class Stats:
    def __init__(self, out_path: str | None) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.conv = Counter()
        self.n_conv = 0
        self.fh = open(out_path, "w", encoding="utf-8") if out_path else None

    def new_conversation(self, long_share: float) -> bool:
        """Count a conversation; True if it gets a long report. Long ones are spread evenly: with 0.1, every 10th."""
        with self.lock:
            k = self.n_conv
            self.n_conv += 1
            self.conv["started"] += 1
        return int((k + 1) * long_share) > int(k * long_share)

    def add(self, rec: dict) -> None:
        with self.lock:
            self.calls.append(rec)
            if self.fh:
                self.fh.write(json.dumps(rec) + "\n")
                self.fh.flush()


def call(url: str, body: dict, headers: dict, timeout: float) -> dict:
    """One streamed chat call. Returns status, timings, usage, the answer text, and the error for a refusal."""
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    t0 = time.time()
    rec: dict = {"t_start": t0}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            first, text, usage = None, [], {}
            for line in r:
                if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                    continue
                c = json.loads(line[6:])
                if c.get("usage"):
                    usage = c["usage"]
                for ch in c.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content") or d.get("reasoning") or d.get("reasoning_content") or d.get("tool_calls"):
                        first = first or time.time()
                    if d.get("content"):
                        text.append(d["content"])
            t1 = time.time()
            rec.update(status=200, ttft_s=None if first is None else first - t0, e2e_s=t1 - t0, text="".join(text),
                       prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                       cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error") or {}
        except (json.JSONDecodeError, AttributeError, ValueError):
            err = {}
        rec.update(status=e.code, e2e_s=time.time() - t0, reason=err.get("code") or err.get("type") or "",
                   retry_after=float(e.headers.get("Retry-After") or 0), message=(err.get("message") or "")[:200])
    except (OSError, ValueError) as e:  # connection reset, timeout, broken stream
        rec.update(status=0, e2e_s=time.time() - t0, reason="client_error", message=repr(e)[:200])
    return rec


def user(i: int, args: argparse.Namespace, stats: Stats, stop_at: float) -> None:
    rng = random.Random(f"{args.seed}-{i}")
    tenant = f"tenant-{i % args.tenants:03d}"
    time.sleep(args.ramp * i / max(1, args.users))
    conv_n = 0
    while time.time() < stop_at:
        conv_n += 1
        long = stats.new_conversation(args.long_share)
        report, sections = make_report(rng, args.long_words if long else args.words)
        conv_id = f"u{i:03d}-c{conv_n}"
        messages = [{"role": "system", "content": SYSTEM}]
        finished = True
        for turn in range(1, args.turns + 1):
            if time.time() >= stop_at:
                finished = False
                break
            q = rng.choice(QUESTIONS).format(a=rng.randint(1, sections), b=rng.randint(1, sections))
            messages.append({"role": "user", "content": f"Here is the report.\n\n{report}\n\n{q}" if turn == 1 else q})
            body = {"model": args.model, "messages": messages, "stream": True, "max_tokens": args.max_tokens}
            body["chat_template_kwargs"] = {"enable_thinking": args.thinking}
            for attempt in range(args.retries + 1):
                headers = {"x-tenant": tenant, "x-request-id": str(uuid.uuid4())}
                rec = call(args.url, body, headers, args.timeout)
                rec.update(user=i, tenant=tenant, conversation=conv_id, turn=turn, attempt=attempt, long_report=long)
                text = rec.pop("text", None)
                stats.add(rec)
                if rec["status"] in (429, 503) and attempt < args.retries:
                    time.sleep(max(rec.get("retry_after") or 1, 0.5) * (1 + rng.random() * 0.5))
                    continue
                break
            if rec["status"] != 200:
                finished = False
                stats.conv["abandoned"] += 1
                # back off before the next conversation, or a user that is always refused hammers the gateway
                time.sleep(max(rec.get("retry_after") or 1, 1) * (1 + rng.random()))
                break
            messages.append({"role": "assistant", "content": text or "(no answer)"})
            time.sleep(args.think_time * rng.random())
        if finished:
            stats.conv["completed"] += 1


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def fmt(x: float | None, unit: str = "s", nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f} {unit}".strip()


def summary(stats: Stats, args: argparse.Namespace, wall: float) -> None:
    calls = stats.calls
    ok = [c for c in calls if c["status"] == 200]
    refused = Counter(f"{c['status']} {c.get('reason', '')}".strip() for c in calls if c["status"] in (429, 503))
    errors = Counter(f"{c['status']} {c.get('reason', '')}".strip() for c in calls if c["status"] not in (200, 429, 503))
    print(f"\n== load test: {args.users} users, {args.tenants} tenants, {wall:.0f} s, long-report share {args.long_share:g}, "
          f"thinking {'on' if args.thinking else 'off'}, max_tokens {args.max_tokens} ==")
    print(f"calls        {len(calls)} sent, {len(ok)} ok, {sum(refused.values())} refused, {sum(errors.values())} errors")
    for k, v in refused.most_common():
        print(f"  refused    {k}: {v}")
    for k, v in errors.most_common():
        print(f"  error      {k}: {v}")
    retried_ok = sum(1 for c in ok if c["attempt"] > 0)
    print(f"retries      {sum(1 for c in calls if c['attempt'] > 0)} retried calls, {retried_ok} of them succeeded")
    cut = stats.conv["started"] - stats.conv["completed"] - stats.conv["abandoned"]
    print(f"conversations {stats.conv['started']} started, {stats.conv['completed']} completed, {stats.conv['abandoned']} given up after "
          f"refusals or errors, {cut} cut short by the end of the test")
    if not ok:
        return
    ttft = [c["ttft_s"] for c in ok if c.get("ttft_s") is not None]
    e2e = [c["e2e_s"] for c in ok]
    tpot_ms = [1000 * (c["e2e_s"] - c["ttft_s"]) / (c["completion_tokens"] - 1) for c in ok
               if c.get("ttft_s") is not None and (c.get("completion_tokens") or 0) > 1]
    tps = [c["completion_tokens"] / (c["e2e_s"] - c["ttft_s"]) for c in ok
           if c.get("ttft_s") is not None and (c.get("completion_tokens") or 0) > 1 and c["e2e_s"] > c["ttft_s"]]
    out_tok = sum(c.get("completion_tokens") or 0 for c in ok)
    prompt = sum(c.get("prompt_tokens") or 0 for c in ok)
    cached = sum(c.get("cached_tokens") or 0 for c in ok)
    print(f"TTFT         p50 {fmt(pct(ttft, .5))}   p90 {fmt(pct(ttft, .9))}   p99 {fmt(pct(ttft, .99))}")
    print(f"E2E          p50 {fmt(pct(e2e, .5))}   p90 {fmt(pct(e2e, .9))}   p99 {fmt(pct(e2e, .99))}")
    print(f"TPOT         p50 {fmt(pct(tpot_ms, .5), 'ms', 1)}   p90 {fmt(pct(tpot_ms, .9), 'ms', 1)}")
    print(f"tokens/s     per call p50 {fmt(pct(tps, .5), '', 1)}, all calls {out_tok / wall:.0f} output tokens/s")
    print(f"prompt       {prompt:,} tokens, {cached / prompt if prompt else 0:.0%} served from the prefix cache")
    print(f"over SLO     {sum(1 for x in e2e if x > args.slo)} of {len(e2e)} ok calls took longer than {args.slo:g} s")
    for name, sel in (("short", lambda c: (c.get("prompt_tokens") or 0) <= args.long_tokens),
                      ("long", lambda c: (c.get("prompt_tokens") or 0) > args.long_tokens)):
        xs = [c["e2e_s"] for c in ok if sel(c)]
        if xs:
            print(f"  {name:5s} prompts ({'<=' if name == 'short' else '>'}{args.long_tokens} tokens): {len(xs)} calls, "
                  f"E2E p50 {fmt(pct(xs, .5))}, p90 {fmt(pct(xs, .9))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080", help="gateway base URL")
    ap.add_argument("--model", default="agent", help="served model name (config.sh SERVED_MODEL_NAME)")
    ap.add_argument("--users", type=int, default=30, help="users chatting at the same time (concurrency)")
    ap.add_argument("--tenants", type=int, default=30, help="users are spread over this many tenants (x-tenant)")
    ap.add_argument("--duration", type=float, default=180, help="seconds; no new call starts after this")
    ap.add_argument("--ramp", type=float, default=0, help="start the users evenly over this many seconds (0 = all at once)")
    ap.add_argument("--turns", type=int, default=4, help="turns per conversation")
    ap.add_argument("--words", type=int, default=1500, help="report size of a normal conversation (~2k tokens)")
    ap.add_argument("--long-words", type=int, default=9000, help="report size of a long conversation (~13k tokens)")
    ap.add_argument("--long-share", type=float, default=0.1, help="share of conversations with a long report")
    ap.add_argument("--max-tokens", type=int, default=1024, help="max output tokens per call (thinking included)")
    ap.add_argument("--thinking", action="store_true", help="let the model think (long calls: raise the SLO first)")
    ap.add_argument("--think-time", type=float, default=2.0, help="max random pause between turns, seconds")
    ap.add_argument("--retries", type=int, default=3, help="retries of a refused call (429 / 503) after Retry-After")
    ap.add_argument("--timeout", type=float, default=600, help="per-call timeout, seconds")
    ap.add_argument("--slo", type=float, default=26, help="only for the summary: count ok calls slower than this")
    ap.add_argument("--long-tokens", type=int, default=10000, help="only for the summary: short/long split, prompt tokens")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", help="write one JSON line per call to this file")
    args = ap.parse_args()

    stats = Stats(args.out)
    t0 = time.time()
    stop_at = t0 + args.ramp + args.duration
    threads = [threading.Thread(target=user, args=(i, args, stats, stop_at), daemon=True) for i in range(args.users)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(10)
            ok = sum(1 for c in stats.calls if c["status"] == 200)
            print(f"[{time.time() - t0:5.0f} s] calls {len(stats.calls)}, ok {ok}, refused "
                  f"{sum(1 for c in stats.calls if c['status'] in (429, 503))}", flush=True)
    except KeyboardInterrupt:
        print("interrupted: summary of the calls so far")
    summary(stats, args, time.time() - t0)


if __name__ == "__main__":
    main()
