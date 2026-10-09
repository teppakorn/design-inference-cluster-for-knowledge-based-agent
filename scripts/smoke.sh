#!/usr/bin/env bash
# End-to-end check through the gateway: model list, a streamed answer, a tool call, a prefix-cache
# hit, an admission refusal, and proof that the KV really moved between pods (NIXL pushes on the
# prefill pods, Mooncake puts, Mooncake master keys).
#   bash scripts/smoke.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config.sh"
GW="${GATEWAY:-http://127.0.0.1:$GATEWAY_PORT}"

echo "== gateway =="
curl -sf "$GW/health"; echo
curl -sf "$GW/v1/models" | python3 -c 'import json,sys; print("models:", [m["id"] for m in json.load(sys.stdin)["data"]])'

python3 - "$GW" "$SERVED_MODEL_NAME" <<'PY'
import json, sys, time, urllib.error, urllib.request
gw, model = sys.argv[1], sys.argv[2]

def post(body, stream=False, headers=None):
    req = urllib.request.Request(gw + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-tenant": "smoke", **(headers or {})})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        if not stream:
            return json.load(r), time.time() - t0, None
        first, text, usage = None, "", None
        for line in r:
            if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                continue
            c = json.loads(line[6:])
            if c.get("usage"):
                usage = c["usage"]
            for ch in c.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    first = first or time.time()
                    text += d["content"]
        return {"text": text, "usage": usage}, time.time() - t0, (first - t0 if first else None)

# a shared prefix of ~1,700 tokens: prefix caching works in whole KV blocks (Qwen3.5 on vLLM: 528 tokens)
system = "You are a helpful assistant for a small online shop. " * 150
NT = {"chat_template_kwargs": {"enable_thinking": False}}  # plumbing check: no thinking
out, dt, ttft = post({**NT, "model": model, "stream": True, "max_tokens": 48, "temperature": 0,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": "Say hello in five words."}]}, stream=True)
print(f"stream   ttft={ttft:.3f}s e2e={dt:.3f}s usage={out['usage']}")
print("         text:", out["text"][:120].replace("\n", " "))
assert out["text"], "empty streamed answer"

tools = [{"type": "function", "function": {"name": "get_order_status", "description": "Look up the status of an order",
          "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}}]
out, dt, _ = post({**NT, "model": model, "max_tokens": 128, "temperature": 0, "tools": tools,
                   "messages": [{"role": "user", "content": "Where is my order A-1042?"}]})
calls = out["choices"][0]["message"].get("tool_calls") or []
print(f"tools    e2e={dt:.3f}s finish={out['choices'][0]['finish_reason']} tool_calls={[c['function'] for c in calls]}")
assert calls, "the model did not call the tool (check --enable-auto-tool-choice and the tool parser)"

out, dt, ttft = post({**NT, "model": model, "stream": True, "max_tokens": 16, "temperature": 0,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": "Say goodbye in five words."}]}, stream=True)
cached = ((out["usage"] or {}).get("prompt_tokens_details") or {}).get("cached_tokens")
print(f"repeat   ttft={ttft:.3f}s cached_tokens={cached}  (same system prompt: expect a prefix-cache hit)")
assert cached, "no prefix-cache hit on a repeated ~1,700-token prefix"

# a deadline no request can meet: admission's slo level must refuse it (when it is on)
adm = json.load(urllib.request.urlopen(gw + "/admin/admission"))
if "slo" in adm["levels"]:
    try:
        post({**NT, "model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}, headers={"x-slo-s": "0.01"})
        raise SystemExit("admission: a 0.01 s deadline was admitted")
    except urllib.error.HTTPError as e:
        err = json.load(e)["error"]
        print(f"deadline {e.code} {err['code']} retry-after={e.headers.get('Retry-After')} est_total={err['est']['est_total']} s")
        assert e.code == 429 and err["code"] == "deadline_unmeetable"
else:
    print(f"deadline (admission levels {adm['levels']}: slo off, not checked)")
PY

echo "== KV moved between pods =="
PAT='^vllm:(nixl_xfer_time_seconds_count|nixl_bytes_transferred_sum|nixl_num_failed_transfers_total|external_prefix_cache_hits_total|external_prefix_cache_queries_total)'
# host-network prefill pods share the node IP, one port each
for p in $(kubectl get pods -l app=vllm-prefill -o jsonpath='{range .items[*]}{.status.podIP}:{.spec.containers[0].ports[?(@.name=="http")].containerPort} {end}'); do
  echo "prefill $p: $(curl -s "http://$p/metrics" | grep -E "$PAT" | sed 's/{[^}]*}//' | tr '\n' ' ' | cut -c1-400)"
  echo "   mooncake: $(curl -s "http://$p/metrics" | grep -E '^vllm:mooncake_store_operation_total' | sed -E 's/.*operation="([^"]+)",status="([^"]+)"[^ ]* /\1:\2=/' | tr '\n' ' ')"
done
MC="$(kubectl get pod -l app=mooncake-master -o jsonpath='{.items[0].status.podIP}')"
echo "mooncake master: $(curl -s "http://$MC:9003/metrics" | grep -E '^master_(key_count|allocated_bytes|total_capacity_bytes) ' | tr '\n' ' ')"
kubectl logs deploy/vllm-decode -c routing-proxy --tail=2 || true
echo "SMOKE PASS"
