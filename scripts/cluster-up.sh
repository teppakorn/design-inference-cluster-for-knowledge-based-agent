#!/usr/bin/env bash
# One-time setup of the GPU box: k3s (NVIDIA runtime) + helm + HAMi (GPU split in 4)
# + Prometheus + Grafana + DCGM exporter + dashboards, then pre-pulls the images.
# Safe to re-run: every step checks before it installs.
#   bash scripts/cluster-up.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found: run this on the GPU box (NVIDIA driver installed)"; exit 1; }
[[ -f "$ROOT/.env" ]] && { set -a; source "$ROOT/.env"; set +a; }

echo "== GPU =="
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader

if [[ ! -x /usr/local/bin/k3s && ! -x /usr/bin/k3s ]]; then
  echo "== k3s (nvidia as the default container runtime) =="
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode 644 --default-runtime nvidia" sh -
else
  echo "== k3s already installed =="
fi
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
grep -q KUBECONFIG ~/.bashrc 2>/dev/null || echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc

# a fresh k3s answers `get nodes` with an empty list for a few seconds
until kubectl get nodes -o name 2>/dev/null | grep -q '^node/'; do sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=180s

if ! command -v helm >/dev/null 2>&1; then
  echo "== helm =="
  curl -sfL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
command -v envsubst >/dev/null || sudo apt-get install -y gettext-base >/dev/null

NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
kubectl label node "$NODE" gpu=on --overwrite

echo "== HAMi (each GPU split into 4 slices) =="
READY_SCHED="$(kubectl -n kube-system get deploy hami-scheduler -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
if [[ "${READY_SCHED:-0}" -ge 1 ]]; then
  echo "HAMi scheduler already ready, skipping"
else
  helm repo add hami-charts https://project-hami.github.io/HAMi/ >/dev/null
  helm repo update hami-charts >/dev/null
  K8S_VERSION="$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"].split("+")[0])')"
  helm upgrade --install hami hami-charts/hami \
    --version "${HAMI_VERSION:-2.9.0}" \
    --namespace kube-system \
    --set scheduler.kubeScheduler.image.registry=registry.k8s.io \
    --set scheduler.kubeScheduler.image.repository=kube-scheduler \
    --set "scheduler.kubeScheduler.image.tag=${K8S_VERSION}" \
    --set "scheduler.kubeScheduler.imageTag=${K8S_VERSION}" \
    --set devicePlugin.deviceSplitCount=4 \
    --wait --timeout 10m
fi

echo "== Prometheus + Grafana + DCGM exporter =="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update prometheus-community grafana >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus \
  --namespace monitoring --create-namespace \
  --values "$ROOT/k8s/monitoring/prometheus-values.yaml" \
  --wait --timeout 10m
helm upgrade --install grafana grafana/grafana \
  --namespace monitoring \
  --values "$ROOT/k8s/monitoring/grafana-values.yaml" \
  --wait --timeout 10m
kubectl apply -f "$ROOT/k8s/monitoring/dcgm-exporter.yaml"
bash "$ROOT/scripts/dashboards.sh" || echo "(dashboards skipped: run bash scripts/dashboards.sh again)"

echo "== pre-pull images (the vLLM image is ~9 GB) =="
source "$ROOT/config.sh"
for img in "$VLLM_IMAGE" "$SIDECAR_IMAGE" "$MOONCAKE_IMAGE" "$GATEWAY_IMAGE" "$WEBUI_IMAGE"; do
  echo "pull $img"
  sudo k3s crictl pull "$img" >/dev/null || echo "  (pull failed: $img; the pod will retry when it starts)"
done
sudo mkdir -p "$HF_CACHE_DIR"

echo
kubectl -n kube-system get pods | grep -E 'hami|NAME' || true
kubectl -n monitoring get pods
echo
echo "Cluster is up. Grafana admin password:"
echo "  kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo"
echo "Next: bash scripts/deploy.sh"
