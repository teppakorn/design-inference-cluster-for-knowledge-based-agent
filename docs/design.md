# Design notes: how it works and why

## Why prefill/decode disaggregation on one GPU

Prefill (reading the prompt) is compute-heavy and bursty. Decode (writing the answer token by token) is memory-bound and steady.
In one engine, a big prefill makes every running decode wait for that step, so inter-token latency jumps.
Here prefill and decode run in separate vLLM engines. A long prompt only slows the prefill pods, and the decode pods keep a steady pace.

The four engines share **one** H100, split by [HAMi](https://github.com/Project-HAMi/HAMi) into 4 slices of 16 GiB and 25% of the SMs.
Sharing one GPU is what makes the KV hand-off cheap: the prefill pod can write into the decode pod's GPU memory over CUDA IPC.

## The KV hand-off (NIXL push over CUDA IPC)

The decode pod's [llm-d](https://github.com/llm-d/llm-d) routing sidecar orders two HTTP legs:

1. **Prefill leg.** It sends the request to the prefill pod named in `x-prefiller-host-port`, with `max_tokens=1` and `kv_transfer_params.do_remote_decode=true`.
2. **Decode leg.** It sends the original request to its local decode vLLM, with the `kv_transfer_params` the prefill leg returned.

The bytes move between the two vLLM engines, not through the sidecar. With `NixlPushConnector`:
- the decode engine registers free blocks;
- the prefill engine WRITEs the prompt's KV into them over UCX `cuda_ipc`.

Measured on this setup with a 2.8k-token prompt (472 MB of KV):

| Transport | Bandwidth | TTFT, cached prompt | TTFT, cold prompt |
|---|---|---|---|
| push + CUDA IPC (default) | 12–44 GB/s | 0.11–0.15 s | ~0.7 s |
| pull + host buffer + TCP | 0.39 GB/s | ~0.47 s | ~1.3 s |
| pull + GPU buffer + TCP | 0.1 GB/s | ~0.31 s | ~5 s |

CUDA IPC needs the pods to share the host IPC and PID namespaces (`PD_HOST_NS=true`).

**Use vLLM v0.31.0 or newer.** In v0.30, a push prefill kept every request's blocks until the 30 s `kv_lease_duration` ran out.
The prefill pods' KV filled up and requests queued behind it. v0.31.0 frees the blocks as soon as the WRITE completes.

## The shared prefix cache (Mooncake)

Each prefill vLLM runs a `MultiConnector` with two connectors:
- **NIXL** hands the KV to decode.
- **MooncakeStoreConnector** saves every computed block to a shared pool and loads prefix hits from it.

Each prefill pod lends 40 GB of host RAM to the pool, so the pool holds 80 GB in total.
A conversation's next turn usually starts with the whole previous turn as its prefix. If the router sends that turn to the other prefill pod, Mooncake still has the blocks.
`kv_load_failure_policy=recompute` means a block evicted before it is loaded is recomputed instead of failing the request.

Decode has no Mooncake. Running NIXL and a second loading connector together on decode can race, so decode only reuses its own GPU prefix cache.

**RDMA needs the host network.** RoCE resolves a queue pair's address through the host network device (for example `eno1`), and a pod's network namespace does not have it.
From a pod, every queue pair fails with `Failed to modify QP to RTR`. So with `MOONCAKE_RDMA=1`, the two prefill pods run on the host network.
They share the node IP, so each one is its own Deployment with its own ports: HTTP 18100/18101 and NIXL 15600/15601.
On a host benchmark, RDMA moved Mooncake blocks 11–19× faster than TCP.

## The gateway

### Why the stages are separate

Each stage answers one question, and each is in its own file:

| Stage | Question | File |
|---|---|---|
| admission | "Can we serve this at all, in time?" | [`gateway/admission.py`](../gateway/admission.py) |
| queue | "Who goes next?" | [`gateway/queue.py`](../gateway/queue.py) |
| router | "Where does it run?" | [`gateway/router.py`](../gateway/router.py) |

[`gateway/server.py`](../gateway/server.py) ties them together, streams the answer, and times every request.

### Slots and late binding

The queue gives each decode pod `MAX_NUM_SEQS` slots, the same number as vLLM's `--max-num-seqs`. So vLLM's own waiting queue stays short, and the order is decided in the gateway.
The router picks a decode pod only when a slot frees up, and only among pods with a free slot (late binding). The cost is affinity: a follow-up turn lands on the pod that cached its history less often.

### Token estimate

The gateway has no tokenizer. It estimates prompt tokens as `0.3371 × message bytes + 729`. This line was fitted on 1,233 real agent calls, with 2.2% median error.
The estimate decides the prompt class (short or long) and feeds admission. Every trace line has both `est_tokens` and the real `prompt_tokens`, so you can fit the line again for your own traffic (`TOKENS_PER_CHAR`, `TOKENS_BASE`).

### Two-class queue

The queue uses smooth weighted round-robin (as in nginx) over the classes that have work:
- each pick adds every waiting class's weight to its credit;
- the class with the highest credit goes;
- that class pays the sum of the waiting classes' weights.

With 9:1 and both classes waiting, the order is `S S S S S L S S S S`. A class with nothing waiting does not build up credit.

### Prefix-load router

At every message boundary the router hashes the prompt so far: tools + messages, serialised with sorted keys. It remembers which pod served each hash.
The longest remembered prefix approximates "this pod has the most of this prompt in its KV cache".
To stop one shared system prompt from pulling all traffic to one pod, a pod is only eligible while its in-flight count stays under ceil(mean × 1.25). This idea comes from consistent hashing with bounded loads (Mirrokni et al., 2018).
In a pilot without the bound, one prefill pod took 63% of all requests.

### Admission: the drain rate

The SLO check uses the formula `est_total = queue_wait + n_in / prefill_rate + n_out × ITL`, with `queue_wait = requests ahead ÷ drain rate`.

**First version (`SLO_DRAIN=completions`): calls finished in the last 30 s ÷ 30.** This lags a load step. Right after load jumps up, few calls have finished yet.
For example, 1 call in 30 s gives 0.03/s, so 10 waiting requests become a 300 s estimate, and the request is refused.
With 30 agents this refused 12% of the calls, all in the first 4–6 s of each run ([results](results.md#admission-slo-26-s)).
How much it hurts depends on how long calls run, because that sets how soon the first calls finish.
With the load test (short answers), it refused 5 calls in the first 10 s. With long thinking calls and retries 2 s apart, it refused every call for the first 40 s, because the retries met the same lag.

**Default (`SLO_DRAIN=littles`): Little's law.** In a steady state, in service = throughput × service time. So `drain = requests in service on the decode pods ÷ mean service time of the last 200 calls`.
It uses the requests in service right now, so it is already about right at the first finished call. In the same load test it refused nothing (0 of 711 calls).
The first calls to finish are usually the short ones, which makes the mean service time low and the drain rate high. That errs toward admitting, which is the safe side.

### Admission: other inputs to improve

1. **Requests that only wait for KV.** vLLM's waiting count also includes requests that are only waiting for their KV to arrive (reason `deferred`), not for capacity. Counting only `capacity` would be more accurate.
2. **Output length is unknown.** `n_out` is the median output of the same prompt class over the last 5 minutes. Without a time limit, a stretch of long answers kept the estimate high, and refused calls never finish to bring it down again.
   For thinking models with very different questions, a history per tenant or per route would be better.
3. **Work that can never meet the SLO** is refused with the same `Retry-After: 2` as overload, but a retry cannot help it. A separate reason (or no `Retry-After`) would tell clients not to retry.

The other two levels are simple and robust:
- The **token bucket** charges est. prompt + min(`max_tokens`, 1024) up front, then settles to the real usage. Charging the full `max_tokens` (10,000 for a thinking agent) would starve tenants that usually write a few hundred tokens.
- The **fleet** check fails open when its data is stale, so a stuck scraper never blocks traffic.
