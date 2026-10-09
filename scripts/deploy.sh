#!/usr/bin/env bash
# Deploy the whole stack: model download, Mooncake master, 2 prefill + 2 decode vLLM pods, gateway.
# Always starts from nothing (a running stack is removed first), so every deploy gets cold engines,
# an empty Mooncake pool and an empty router index.
#   bash scripts/deploy.sh
#   ADMISSION=off QUEUE_POLICY=none bash scripts/deploy.sh     # any config.sh value can be overridden
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config.sh"
cd "$ROOT"
command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found: run this on the GPU box"; exit 1; }
command -v envsubst >/dev/null || sudo apt-get install -y gettext-base >/dev/null
[[ -n "$GPU_UUID" ]] || { echo "no GPU at GPU_INDEX=$GPU_INDEX"; exit 1; }

echo "== remove the running stack (it holds the GPU slices) =="
bash "$ROOT/scripts/teardown.sh"

if [[ -n "${HF_TOKEN:-}" ]]; then
  kubectl create secret generic hf-token --from-literal=token="$HF_TOKEN" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi

echo "== model weights -> $HF_CACHE_DIR ($MODEL) =="
kubectl delete job model-fetch --ignore-not-found >/dev/null
render k8s/model-fetch.yaml | kubectl apply -f -
kubectl wait --for=condition=complete job/model-fetch --timeout=30m

echo "== $MODEL on GPU $GPU_INDEX: 2 prefill + 2 decode, ${GPU_MEM_MIB} MiB / ${GPU_CORES}% SMs each =="
echo "   max running seqs=$MAX_NUM_SEQS, chunked prefill=$MAX_NUM_BATCHED_TOKENS tokens, max model len=$MAX_MODEL_LEN"
echo "   prefill kv: $PREFILL_KV_CONFIG"
echo "   decode  kv: $DECODE_KV_CONFIG"
echo "   Mooncake: $MOONCAKE_PROTOCOL ${MOONCAKE_DEVICE}, ${MOONCAKE_SEGMENT} per prefill pod"
render k8s/mooncake.yaml | kubectl apply -f -
kubectl rollout status deploy/mooncake-master --timeout=5m
for i in "${!PREFILL_DEPLOYS[@]}"; do prefill_set "$i"; render k8s/vllm-prefill.yaml | kubectl apply -f -; done
render k8s/vllm-decode.yaml | kubectl apply -f -

echo "== gateway: admission=$ADMISSION (SLO ${SLO_S} s) · queue=$QUEUE_POLICY · router=$ROUTER_POLICY · cache_salt=$CACHE_SALT =="
mkdir -p "$ROOT/traces"
render k8s/gateway.yaml | kubectl apply -f -
kubectl rollout status deploy/pd-gateway --timeout=5m

echo "== waiting for vLLM (weights load + compile + CUDA graphs, ~3-6 min) =="
for d in "${PREFILL_DEPLOYS[@]}" vllm-decode; do
  kubectl rollout status "deploy/$d" --timeout=30m
done
sleep 6  # the gateway re-resolves the pods every 5 s
[[ "$WEBUI" == 1 ]] && bash "$ROOT/scripts/webui.sh"
echo
kubectl get pods -o wide -l stack=pd-gateway
echo
curl -s "http://127.0.0.1:$GATEWAY_PORT/debug/pods" | python3 -m json.tool
echo
echo "Deployed. Gateway: http://<node>:$GATEWAY_PORT/v1 (OpenAI API)$([[ "$WEBUI" == 1 ]] && echo ", chat page: http://<node>:$WEBUI_PORT")."
echo "Next: bash scripts/smoke.sh"
