#!/usr/bin/env bash
# Show or change the gateway's admission at run time, no redeploy. A change lasts until the next
# deploy, which starts again from ADMISSION / SLO_S / BUCKET_* / KV_SATURATION in config.sh.
#   bash scripts/admission.sh                 # config, counters, bucket levels, fleet snapshot, estimate inputs
#   bash scripts/admission.sh off             # admit everything
#   bash scripts/admission.sh all             # bucket + fleet + slo
#   bash scripts/admission.sh all 30          # ... with a 30 s SLO
#   bash scripts/admission.sh slo 20          # only the deadline check, 20 s SLO
#   bash scripts/admission.sh bucket,fleet    # any comma list of bucket, fleet, slo
#   bash scripts/admission.sh slo-s 40        # keep the levels, change only the SLO
#   bash scripts/admission.sh out-q 0.9       # n_out = this quantile of recent output lengths
#   bash scripts/admission.sh set '{"bucket_tokens_per_s": 5000, "kv_saturation": 0.9}'
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY_PORT="$(source "$ROOT/config.sh" >/dev/null 2>&1; echo "${GATEWAY_PORT:-8080}")"
GW="${GATEWAY:-http://127.0.0.1:$GATEWAY_PORT}"
post() { curl -sf -X POST "$GW/admin/admission" -H 'Content-Type: application/json' -d "$1" | python3 -m json.tool; }
case "${1:-}" in
  "")    curl -sf "$GW/admin/admission" | python3 -m json.tool ;;
  slo-s) post "{\"slo_s\": ${2:?SLO seconds}}" ;;
  out-q) post "{\"slo_out_quantile\": ${2:?quantile 0..1}}" ;;
  set)   post "${2:?JSON object}" ;;
  *)     post "{\"levels\": \"$1\"${2:+, \"slo_s\": $2}}" ;;
esac
