# All settings in one place. Sourced by scripts/*.sh.
# Override any value in .env (see .env.example) or on the command line:
#   SLO_S=40 bash scripts/deploy.sh
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
[[ -f "$ROOT/.env" ]] && { set -a; source "$ROOT/.env"; set +a; }
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

# ------------------------------------------------------------------ model + images
export MODEL="${MODEL:-Qwen/Qwen3.5-4B}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-agent}"   # the "model" name clients send
# v0.31.0 or newer: a NixlPush prefill frees its KV blocks as soon as the WRITE completes
export VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.31.0}"
export SIDECAR_IMAGE="${SIDECAR_IMAGE:-ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.11.0}"
export MOONCAKE_IMAGE="${MOONCAKE_IMAGE:-docker.io/kvcacheai/mooncake:0.3.13.post1}"
export GATEWAY_IMAGE="${GATEWAY_IMAGE:-python:3.12-slim}"

# ------------------------------------------------------------------ engine
export MAX_NUM_SEQS="${MAX_NUM_SEQS:-9}"                    # running requests per vLLM pod
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-2048}"  # chunked-prefill budget per step
export GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.88}"                 # share of the GPU slice vLLM may use
# THINKING=1: the model thinks by default (clients can still turn it off per request)
export THINKING="${THINKING:-1}"
if [[ "$THINKING" == 1 ]]; then _THINK=true; else _THINK=false; fi
# Sampling added by the gateway to every request that does not set it. Qwen3.5 loops in its
# thinking with greedy decoding, so these are the model card's "thinking mode" values.
if [[ "$THINKING" == 1 ]]; then
  export SAMPLING_DEFAULTS_JSON="${SAMPLING_DEFAULTS_JSON:-{\"temperature\": 1.0, \"top_p\": 0.95, \"top_k\": 20, \"min_p\": 0.0, \"presence_penalty\": 1.5\}}"
else
  export SAMPLING_DEFAULTS_JSON="${SAMPLING_DEFAULTS_JSON:-{\}}"
fi
case "$MODEL" in
  *Qwen3.5*)
    # hybrid attention + vision model: text only, XML tool calls, qwen3 reasoning parser
    export MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
    export TOOL_PARSER="${TOOL_PARSER:-qwen3_coder}"
    VLLM_EXTRA_ARGS=(--language-model-only --reasoning-parser=qwen3 "--default-chat-template-kwargs={\"enable_thinking\": $_THINK}")
    ;;
  *)
    export MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
    export TOOL_PARSER="${TOOL_PARSER:-hermes}"
    VLLM_EXTRA_ARGS=(--block-size=128)
    ;;
esac

# ------------------------------------------------------------------ GPU slices (HAMi)
# One GPU is split into 4 slices: 2 prefill + 2 decode pods. All four must sit on the SAME GPU
# (the KV moves between them over CUDA IPC), so every engine pod is pinned to GPU_INDEX.
export GPU_MEM_MIB="${GPU_MEM_MIB:-16384}"   # memory per slice
export GPU_CORES="${GPU_CORES:-25}"          # % of the SMs per slice
export GPU_INDEX="${GPU_INDEX:-0}"
if [[ -z "${GPU_UUID:-}" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  GPU_UUID="$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i "$GPU_INDEX" 2>/dev/null | tr -d ' ')"
fi
export GPU_UUID="${GPU_UUID:-}"

# ------------------------------------------------------------------ prefill -> decode KV transfer (NIXL)
# The prefill pod WRITEs the prompt's KV straight into the decode pod's GPU memory over CUDA IPC.
# This needs the host IPC + PID namespaces (PD_HOST_NS=true). Fallback without CUDA IPC:
#   NIXL_CONNECTOR=NixlConnector NIXL_BUFFER_DEVICE=cpu UCX_TLS=tcp PD_HOST_NS=false  (~30x slower)
export NIXL_CONNECTOR="${NIXL_CONNECTOR:-NixlPushConnector}"
export NIXL_BUFFER_DEVICE="${NIXL_BUFFER_DEVICE:-cuda}"
export UCX_TLS="${UCX_TLS:-cuda_ipc,cuda_copy,tcp,self}"
export PD_HOST_NS="${PD_HOST_NS:-true}"

# ------------------------------------------------------------------ shared prefix-KV pool (Mooncake)
# Each prefill pod lends MOONCAKE_SEGMENT of host RAM to a pool shared by both prefill pods: a prompt
# prefilled on one pod is a cache hit on the other. Needs 2 x MOONCAKE_SEGMENT of free host RAM.
export MOONCAKE_SEGMENT="${MOONCAKE_SEGMENT:-40GB}"
export MOONCAKE_LOCAL_BUFFER="${MOONCAKE_LOCAL_BUFFER:-2GB}"
# MOONCAKE_RDMA=1 (default): RDMA over the box's RoCE / InfiniBand NIC. Check your device and GID:
#   ls /sys/class/infiniband/                                   -> MOONCAKE_DEVICE (e.g. mlx5_0)
#   cat /sys/class/infiniband/mlx5_0/ports/1/gid_attrs/types/*   -> MOONCAKE_GID_INDEX of "RoCE v2"
# The prefill containers then run privileged (for /dev/infiniband and pinned memory) and on the host
# network (RoCE resolves addresses through the host's network device). MOONCAKE_RDMA=0 -> TCP.
export MOONCAKE_RDMA="${MOONCAKE_RDMA:-1}"
if [[ "$MOONCAKE_RDMA" == 1 ]]; then
  export MOONCAKE_PROTOCOL=rdma MOONCAKE_DEVICE="${MOONCAKE_DEVICE:-mlx5_0}" MOONCAKE_GID_INDEX="${MOONCAKE_GID_INDEX:-3}" MOONCAKE_PRIVILEGED=true
else
  export MOONCAKE_PROTOCOL=tcp MOONCAKE_DEVICE="" MOONCAKE_GID_INDEX=0 MOONCAKE_PRIVILEGED=false
fi
# Host-network prefill: both prefill pods share the node IP, so each one is its own Deployment
# with its own ports (vLLM HTTP, NIXL side channel). prefill_set <i> exports one Deployment's values.
export PREFILL_HOSTNET="${PREFILL_HOSTNET:-$MOONCAKE_RDMA}"
if [[ "$PREFILL_HOSTNET" == 1 ]]; then
  PREFILL_DEPLOYS=(vllm-prefill-0 vllm-prefill-1)
  export PREFILL_TARGETS="vllm-prefill-0-pods:18100,vllm-prefill-1-pods:18101"
else
  PREFILL_DEPLOYS=(vllm-prefill)
  export PREFILL_TARGETS="vllm-prefill-pods:8000"
fi
prefill_set() {
  if [[ "$PREFILL_HOSTNET" == 1 ]]; then
    export PREFILL_NAME="vllm-prefill-$1" PREFILL_REPLICAS=1 PREFILL_PORT=$((18100 + $1)) PREFILL_NIXL_PORT=$((15600 + $1)) \
      PREFILL_HOST_NETWORK=true PREFILL_DNS_POLICY=ClusterFirstWithHostNet
  else
    export PREFILL_NAME=vllm-prefill PREFILL_REPLICAS=2 PREFILL_PORT=8000 PREFILL_NIXL_PORT=5600 \
      PREFILL_HOST_NETWORK=false PREFILL_DNS_POLICY=ClusterFirst
  fi
}
prefill_set 0
# vLLM KV connectors. Prefill: NIXL (hand the KV to decode) + MooncakeStore (park / load prefix blocks).
# Decode: NIXL only. recompute = a Mooncake block that is gone by load time is recomputed, not a 500.
_nixl() {
  printf '{"kv_connector":"%s","kv_role":"%s","kv_buffer_device":"%s","kv_connector_extra_config":{"backends":["UCX"]}}' \
    "$NIXL_CONNECTOR" "$1" "$NIXL_BUFFER_DEVICE"
}
PREFILL_KV_CONFIG="$(printf \
  '{"kv_connector":"MultiConnector","kv_role":"kv_both","kv_load_failure_policy":"recompute","kv_connector_extra_config":{"connectors":[%s,{"kv_connector":"MooncakeStoreConnector","kv_role":"kv_both"}]}}' \
  "$(_nixl kv_producer)")"
DECODE_KV_CONFIG="$(_nixl kv_consumer | sed 's/^{/{"kv_load_failure_policy":"recompute",/')"
export PREFILL_KV_CONFIG DECODE_KV_CONFIG

# ------------------------------------------------------------------ gateway: admission -> queue -> router
# Router (gateway/router.py): prefix-load | least-loaded | round-robin | random
export ROUTER_POLICY="${ROUTER_POLICY:-prefix-load}"
# Queue (gateway/queue.py): two-class | fcfs | tenant-rr | none. A slot = one running request on a
# decode pod; MAX_NUM_SEQS slots per decode pod. two-class: prompts above LONG_PROMPT_TOKENS (estimated)
# are "long"; while both classes wait, short:long are served QUEUE_CLASS_WEIGHTS (9 short, then 1 long).
export QUEUE_POLICY="${QUEUE_POLICY:-two-class}"
export LONG_PROMPT_TOKENS="${LONG_PROMPT_TOKENS:-10000}"
export QUEUE_CLASS_WEIGHTS="${QUEUE_CLASS_WEIGHTS:-9:1}"
# Admission (gateway/admission.py): all | off | a comma list of bucket,fleet,slo
#   bucket  token bucket per tenant (x-tenant header)              -> 429 tenant_tokens
#   fleet   every pod of a pool has KV usage >= KV_SATURATION      -> 503 kv_free
#   slo     estimated finish later than SLO_S after arrival         -> 429 deadline_unmeetable
# Change it at run time without a redeploy: bash scripts/admission.sh
export ADMISSION="${ADMISSION:-all}"
export SLO_S="${SLO_S:-26}"
export SLO_OUT_QUANTILE="${SLO_OUT_QUANTILE:-0.5}"   # n_out = this quantile of recent output lengths
# how fast the queue drains: littles = in service / mean service time; completions = calls finished in the
# last 30 s (lags a load step and over-refuses right after it; kept to reproduce earlier measurements)
export SLO_DRAIN="${SLO_DRAIN:-littles}"
export BUCKET_TOKENS_PER_S="${BUCKET_TOKENS_PER_S:-3000}"
export BUCKET_BURST="${BUCKET_BURST:-60000}"
export BUCKET_OUTPUT_RESERVE="${BUCKET_OUTPUT_RESERVE:-1024}"
export KV_SATURATION="${KV_SATURATION:-0.80}"
# CACHE_SALT=tenant: each tenant gets its own prefix cache (GPU cache, Mooncake keys and the router's
# index all split by tenant). off = all tenants share cached prefixes (e.g. a common system prompt).
export CACHE_SALT="${CACHE_SALT:-off}"
export GATEWAY_PORT="${GATEWAY_PORT:-8080}"   # host port of the gateway on the node
export HOST_ROOT="${HOST_ROOT:-$ROOT}"        # the gateway pod runs the code from this folder
# GATEWAY_TRACE=1: one JSON line per request in traces/gateway.jsonl (timings, tokens, pods, admission estimate)
export GATEWAY_TRACE="${GATEWAY_TRACE:-1}"
if [[ "$GATEWAY_TRACE" == 1 ]]; then export TRACE_DIR=/lab/traces; else export TRACE_DIR=""; fi
# Open WebUI (scripts/webui.sh): a chat page in front of the gateway, on node port WEBUI_PORT.
# WEBUI_AUTH=false: no login (fine behind an SSH tunnel); true: accounts, the first sign-up is the admin.
# Switching from false to true later needs an empty WEBUI_DATA_DIR.
export WEBUI="${WEBUI:-1}"
export WEBUI_IMAGE="${WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.11.4}"
export WEBUI_PORT="${WEBUI_PORT:-30030}"
export WEBUI_AUTH="${WEBUI_AUTH:-false}"
export WEBUI_DATA_DIR="${WEBUI_DATA_DIR:-/var/lib/open-webui}"
# WIKI_DIR: a host folder of Markdown notes the chat model can search (wiki_tools/, k8s/wiki-tools.yaml).
# Empty = no wiki tools. Try it with the made-up sample: WIKI_DIR=$PWD/wiki_tools/sample bash scripts/webui.sh
# WIKI_DESCRIPTION says what the notes are, for the system prompt ("a local knowledge wiki of N <description>").
export WIKI_DIR="${WIKI_DIR:-}"
export WIKI_DESCRIPTION="${WIKI_DESCRIPTION:-notes}"
export WEBUI_CONFIG_HASH="${WEBUI_CONFIG_HASH:-}"
# model weights are downloaded once into this host folder and shared by all vLLM pods
export HF_CACHE_DIR="${HF_CACHE_DIR:-/var/cache/huggingface}"

# ------------------------------------------------------------------ templating
# Optional CLI args become YAML list items at the manifests' 12-space indent
_yaml_args() {
  local out="" a
  for a in "$@"; do
    [[ -z "$out" ]] && out="- '${a}'" || out+=$'\n'"            - '${a}'"
  done
  printf '%s' "$out"
}
export VLLM_EXTRA_ARGS_YAML="$(_yaml_args "${VLLM_EXTRA_ARGS[@]}")"

TEMPLATE_VARS='${GPU_UUID} ${MODEL} ${SERVED_MODEL_NAME} ${VLLM_IMAGE} ${SIDECAR_IMAGE} ${MOONCAKE_IMAGE} ${GATEWAY_IMAGE} ${MAX_NUM_SEQS} ${MAX_NUM_BATCHED_TOKENS} ${MAX_MODEL_LEN} ${GPU_MEM_UTIL} ${GPU_MEM_MIB} ${GPU_CORES} ${UCX_TLS} ${PD_HOST_NS} ${TOOL_PARSER} ${VLLM_EXTRA_ARGS_YAML} ${MOONCAKE_SEGMENT} ${MOONCAKE_LOCAL_BUFFER} ${MOONCAKE_PROTOCOL} ${MOONCAKE_DEVICE} ${MOONCAKE_GID_INDEX} ${MOONCAKE_PRIVILEGED} ${PREFILL_KV_CONFIG} ${DECODE_KV_CONFIG} ${PREFILL_NAME} ${PREFILL_REPLICAS} ${PREFILL_PORT} ${PREFILL_NIXL_PORT} ${PREFILL_HOST_NETWORK} ${PREFILL_DNS_POLICY} ${PREFILL_TARGETS} ${SAMPLING_DEFAULTS_JSON} ${ROUTER_POLICY} ${QUEUE_POLICY} ${LONG_PROMPT_TOKENS} ${QUEUE_CLASS_WEIGHTS} ${ADMISSION} ${SLO_S} ${SLO_OUT_QUANTILE} ${SLO_DRAIN} ${BUCKET_TOKENS_PER_S} ${BUCKET_BURST} ${BUCKET_OUTPUT_RESERVE} ${KV_SATURATION} ${CACHE_SALT} ${GATEWAY_PORT} ${HOST_ROOT} ${TRACE_DIR} ${HF_CACHE_DIR} ${WEBUI_IMAGE} ${WEBUI_PORT} ${WEBUI_AUTH} ${WEBUI_DATA_DIR} ${WIKI_DIR} ${WIKI_DESCRIPTION} ${WEBUI_CONFIG_HASH}'
render() { envsubst "$TEMPLATE_VARS" < "$1"; }
