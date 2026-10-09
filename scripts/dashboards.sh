#!/usr/bin/env bash
# Regenerate the Grafana dashboards (monitoring/dashboards.py) and load them through the
# Grafana sidecar (a ConfigMap labelled grafana_dashboard=1).
#   bash scripts/dashboards.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
python3 -m monitoring.dashboards
kubectl -n monitoring create configmap pd-gateway-dashboards \
  --from-file="$ROOT/k8s/monitoring/dashboards" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring label configmap pd-gateway-dashboards grafana_dashboard=1 --overwrite
