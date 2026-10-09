#!/usr/bin/env bash
# Open WebUI, a chat page in front of the gateway. scripts/deploy.sh starts it too (WEBUI=1).
# With WIKI_DIR set, it also starts wiki-tools and gives the model its tools (search_wiki, read_note,
# list_tags) and a system prompt that describes the wiki. Run it again after the wiki changes.
# It holds no GPU, so scripts/teardown.sh leaves it running; your chats stay in WEBUI_DATA_DIR.
#   bash scripts/webui.sh                                   # start or update it
#   WIKI_DIR=$PWD/wiki_tools/sample bash scripts/webui.sh   # ... with the sample wiki
#   bash scripts/webui.sh down                              # remove it (the chats stay on disk)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/config.sh"
cd "$ROOT"
if [[ "${1:-up}" == down ]]; then
  kubectl delete deploy open-webui wiki-tools --ignore-not-found --wait=true
  kubectl delete svc open-webui wiki-tools --ignore-not-found
  kubectl delete configmap open-webui-extra --ignore-not-found
  echo "Open WebUI removed (data kept in $WEBUI_DATA_DIR)"
  exit 0
fi
if [[ -n "$WIKI_DIR" ]]; then
  [[ -d "$WIKI_DIR" ]] || { echo "WIKI_DIR=$WIKI_DIR is not a folder"; exit 1; }
  WIKI_DIR="$(cd "$WIKI_DIR" && pwd)"
  echo "== wiki-tools: $WIKI_DIR =="
  render k8s/wiki-tools.yaml | kubectl apply -f -
  kubectl rollout restart deploy/wiki-tools >/dev/null   # it reads the wiki at start
  kubectl rollout status deploy/wiki-tools --timeout=5m
else
  kubectl delete deploy wiki-tools --ignore-not-found >/dev/null
  kubectl delete svc wiki-tools --ignore-not-found >/dev/null
fi
# the Open WebUI settings that connect the tools (empty without a wiki), one file per env var
EXTRA="$(mktemp -d)"
python3 -m wiki_tools.openwebui "$EXTRA" "$WIKI_DIR" "$WIKI_DESCRIPTION"
kubectl create configmap open-webui-extra --from-file="$EXTRA" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
export WEBUI_CONFIG_HASH="$(cat "$EXTRA"/* | sha1sum | cut -c1-12)"
rm -rf "$EXTRA"
render k8s/open-webui.yaml | kubectl apply -f -
echo "waiting for Open WebUI (the first start takes 1-3 min)"
kubectl rollout status deploy/open-webui --timeout=10m
echo "Open WebUI: http://127.0.0.1:$WEBUI_PORT on the node; from your laptop: ssh -L $WEBUI_PORT:127.0.0.1:$WEBUI_PORT <user>@<gpu-box>"
[[ -n "$WIKI_DIR" ]] && echo "wiki tools on: $(kubectl exec deploy/wiki-tools -- python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health").read().decode())')"
exit 0
