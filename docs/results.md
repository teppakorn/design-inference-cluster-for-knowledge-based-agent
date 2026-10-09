# What we measured

Two sets of measurements:

1. [This repository on the GPU box](#this-repository-load-test): `scripts/deploy.sh` plus `loadtest/loadtest.py`, run to check the finished stack.
2. [The experiments behind its design](#the-experiments-behind-the-design): 30 tool-using agents, three cold runs per setting.

## This repository: load test

1 × H100 PCIe, deployed from scratch with `bash scripts/deploy.sh` (defaults: admission `all`, SLO 26 s, two-class queue, prefix-load router, Mooncake over RDMA).
`python3 loadtest/loadtest.py --users 30`: 30 users chatting at once (30 tenants), 4 turns per conversation, one conversation in ten with a ~13k-token report, thinking off, `max_tokens` 1024.

| Setting | Calls | Refused | TTFT p50 / p90 / p99 | E2E p50 / p90 / p99 | Over 26 s | Output tokens/s |
|---|---|---|---|---|---|---|
| default (`SLO_DRAIN=littles`), 180 s | 711 | **0** | 2.75 / 4.48 / 9.29 s | 5.88 / 10.60 / 16.07 s | 1 | 641 |
| `SLO_DRAIN=completions`, 120 s | 518 | 5, all in the first 10 s (all OK on retry) | 2.43 / 3.95 / 8.66 s | 5.49 / 9.96 / 16.04 s | 1 | 668 |

Other numbers from the default run:
- time per output token p50 20.5 ms;
- 66% of prompt tokens served from the prefix cache;
- long prompts (> 10k tokens): E2E p50 10.2 s against 5.7 s for short ones;
- no pod restarted;
- 132 of the 133 dashboard queries had data (the empty one is the Mooncake failure rate: no failures).

**Admission under pressure.** Three more runs show how the `slo` check behaves:
- **Thinking on, `max_tokens` 2048, old drain (`completions`):** 50 calls OK, 1,745 refused. In the first 40 s, every call was refused because of the drain lag (`queue_wait` 30 s with a drain rate of 0.1/s).
  After that, the median output was 2,048 tokens (the model thought up to the cap). At ~18 ms per token that is about 37 s, so every call was estimated, correctly, above 26 s.
  This is why the load test turns thinking off by default, and why the drain default is now Little's law.
- **SLO 8 s (`bash scripts/admission.sh slo-s 8`):** for the calls it admitted, the estimate matched reality at the median: 5.44 s estimated against 5.42 s measured.
  It refused 1 call, and 17% of admitted calls still took more than 8 s. The estimate uses the median output length, so it cannot tell which calls will write long answers.
- **SLO 8 s and `out-q 0.9`:** `n_out` became the 90th-percentile output length, and the estimate went above 8 s even with no queue. Every call was refused.
  The gateway answered these refusals at about 260 per second without errors.

So pick the SLO from the outputs you expect: at about 20 ms per token, a 26 s SLO fits answers up to roughly 1,000 tokens.

Open WebUI worked through the same gateway: thinking shown as a collapsible block, one gateway call per message, and the Open WebUI user id used as the tenant.
With the SLO at 8 s, a refusal appeared in the chat as "estimated completion 10.3 s > deadline 8 s".

## The experiments behind the design

### Setup

| | |
|---|---|
| Hardware | 1 × H100 PCIe 80 GB in a cloud VM |
| Stack | this one: vLLM v0.31.0 P/D 2 + 2, NIXL push over CUDA IPC, Mooncake over RDMA with 40 GB per prefill pod |
| Model | Qwen3.5-4B, thinking on, `max_tokens` 10,000 |
| Load | 30 tool-using research agents at the same time. Each agent is its own random tenant, with `CACHE_SALT=tenant`. One agent makes about 4–5 LLM calls, and its prompts grow to 10–25k tokens. |
| Runs | 3 cold runs per setting (fresh deploy: empty caches, empty pool), interleaved, after an 8-agent warm-up |
| Statistics | Mann-Whitney U on agent and call latencies. The p-values treat agents as independent, so read them as a guide. |

The agents' client did **not** retry: a refused call ended its agent. This makes refusals look as costly as they can be.
Real clients should retry after `Retry-After`; the [load test](../loadtest/loadtest.py) does.

### Router

| Router | Agent E2E p50 | Decode prefix hit | KV pushed to decode per run |
|---|---|---|---|
| **prefix-load** | 67.0 s | **61%** | 22.0 GB |
| least-loaded | 68.0 s | 50% | 25.4 GB |
| round-robin | 69.4 s | 35% | 31.4 GB |

No router was faster (p = 0.35–0.92 between any two). Prefix-load keeps the most of each agent's history on the decode GPU and moves the least KV.
On prefill the three routers reuse the same share of the prompt (62%), because Mooncake serves whatever the prefill GPU misses.
With a shared cache (no `CACHE_SALT`), prefix-load did beat round-robin (p = 0.03).

### Queue

| Gateway queue | Agent E2E p50 | TTFT p50 (per run) | Decode prefix hit |
|---|---|---|---|
| none | 67.0 s | about 5 s | 61% |
| fcfs | 68.4 s | 5.6–6.0 s | 37–43% |
| tenant-rr | 68.7 s | 6.1–6.9 s | 37–43% |
| **two-class 9:1** | 68.6 s | **4.5–5.3 s** | 37–43% |

No queue policy changed how long the agents took (p ≥ 0.43). The queue moves the waiting from vLLM to the gateway, and it costs decode affinity (61% → 37–43%), because a request can only go to a decode pod with a free slot.
It also takes load off the prefill pods. The two effects roughly cancel out.
Among the queues, two-class gave the lowest TTFT, the shortest runs and the lowest agent p99. Its short prompts waited 3.6 s at the gateway, against 4.2 s under FCFS.
Tenant round-robin behaves like FCFS here, because every tenant had one agent.

#### Six 15k-token prompts at the start

In this test, six extra clients each sent one 15k-token "summarize this report" prompt at the same moment as the agents.

| Gateway queue | Agent E2E p50 |
|---|---|
| none | 93.9 s |
| **two-class 9:1** | **98.5 s** |
| fcfs | 103.4 s |
| tenant-rr | 103.8 s |

The six long prompts slowed every agent by a third to a half, under every policy. They arrived first, found free slots and were never queued.
Then each held a decode sequence for about 130 s, while writing ~5,700 tokens.
Two-class was better than FCFS (p = 0.04) and equal to no queue (p = 0.77). To contain such prompts, the gateway has to act before they take a slot, through admission or reserved capacity.

### Admission (SLO 26 s)

| Admission | Calls refused | Agents finished (no retry) | Calls over 26 s | Agent E2E p50 |
|---|---|---|---|---|
| off | 0 | 86–87 of 90 | ~10% | 67 s |
| **all** (`SLO_DRAIN=completions`) | 35 of 300 (12%) | 54 of 90 | 1.1% | 44.7 s |

- Every refusal from the SLO estimate came in the first 4–6.4 s of a run, on the agents' second call. At that moment the drain rate (calls finished in the last 30 s) was still 0.07–0.3/s, while the real rate reached about 1.5/s a few seconds later. That made `queue_wait` 22–120 s.
  After 6.4 s nothing more was refused, and admitted calls were estimated at 6.1 s p50 and 9.1 s p90.
- The survivors look fast mostly because a third of the load was gone, not because the estimate was right.
- The inputs: inter-token latency measured a steady 20 ms (accurate). The prefill rate swung from 9k to 56k tokens/s, because cache hits count as prefilled tokens. `n_out` was 216 tokens at p50, against 374 in reality.
- **The machine was fine in every setting.** There were no container restarts and no running pod went not-ready over 12 runs. The gateway answered only 200 and its own refusals, never a 5xx from upstream. GPU, CPU and memory looked the same as without admission. The gateway used 0.12–0.16 CPU cores and 44–52 MB.

These runs used the first drain rate (`SLO_DRAIN=completions`). The default is now Little's law; see [design.md](design.md#admission-the-drain-rate).

### Speed of the stack

At C = 30 the stack produced about 610–650 output tokens/s in total. Time per output token was about 22 ms p50, and the p99 gap between tokens was 134–151 ms.
