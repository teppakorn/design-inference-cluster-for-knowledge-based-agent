#!/usr/bin/env bash
# Remove the stack and wait until the GPU slices are free again. Monitoring (Prometheus,
# Grafana) and the downloaded model stay.
#   bash scripts/teardown.sh
#   bash scripts/teardown.sh --k3s     # uninstall k3s completely (rebuild with scripts/cluster-up.sh)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config.sh"

if [[ "${1:-}" == "--k3s" ]]; then
  sudo /usr/local/bin/k3s-uninstall.sh
  echo "k3s removed. The model cache is kept in $HF_CACHE_DIR."
  exit 0
fi

kubectl delete deploy vllm-prefill vllm-prefill-0 vllm-prefill-1 vllm-decode mooncake-master pd-gateway --ignore-not-found --wait=true
kubectl delete svc vllm-prefill-pods vllm-prefill-0-pods vllm-prefill-1-pods vllm-decode-pods mooncake-master pd-gateway --ignore-not-found
kubectl delete configmap mooncake-client --ignore-not-found
for _ in $(seq 1 60); do
  # grep -c prints 0 and exits 1 on no match: keep pipefail from ending the script
  n="$(kubectl get pods -l stack=pd-gateway --no-headers 2>/dev/null | grep -c . || true)"
  [[ "$n" -eq 0 ]] && break
  sleep 3
done
echo "teardown done. GPU now:"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader || true
