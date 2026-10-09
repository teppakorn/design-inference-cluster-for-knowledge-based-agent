#!/usr/bin/env bash
# What is running: pods, GPU memory, the gateway's view (pods, queue, admission) and its request counters.
#   bash scripts/status.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config.sh"
GW="${GATEWAY:-http://127.0.0.1:$GATEWAY_PORT}"
kubectl get pods -o wide -l stack=pd-gateway
echo
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader || true
echo
curl -sf "$GW/debug/pods" | python3 -m json.tool || echo "gateway not answering on $GW"
echo
curl -sf "$GW/metrics" | grep -E '^gw_(requests_total|admitted_total|shed_total)' | grep -v ' 0.0$' || true
