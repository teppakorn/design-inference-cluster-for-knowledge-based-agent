"""Generate the Grafana dashboards (JSON) that scripts/dashboards.sh loads.

    python3 -m monitoring.dashboards  ->  k8s/monitoring/dashboards/pdgw-*.json

Built on the metrics this stack exports:
  gw_*      the gateway: requests, admission (admitted / shed / estimate inputs), queue, routing, TTFT / ITL / E2E, tokens
  vllm:*    the vLLM engines, incl. NIXL (prefill -> decode KV) and the Mooncake store connector
  master_*  the Mooncake master
  hami_*    HAMi GPU slices · DCGM_* GPUs · kube_* / container_* / node_* the cluster
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "k8s" / "monitoring" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
NS = 'namespace="default"'
GPU = 'gpu="0"'  # the engine slices are pinned to one GPU (config.sh GPU_INDEX)


def panel(pid: int, title: str, exprs: list[tuple[str, str]], x: int, y: int, unit: str = "short", w: int = 12, h: int = 8,
          desc: str = "") -> dict:
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"fillOpacity": 8, "lineWidth": 1, "showPoints": "never"}}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "desc"}},
        "targets": [{"refId": chr(65 + i), "datasource": PROM, "expr": e, "legendFormat": lf} for i, (e, lf) in enumerate(exprs)],
    }


def stat(pid: int, title: str, expr: str, x: int, y: int, unit: str = "short", w: int = 4, h: int = 4, legend: str = "",
         decimals: int | None = None) -> dict:
    p = panel(pid, title, [(expr, legend)], x, y, unit, w, h)
    p["type"] = "stat"
    p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                    "graphMode": "area", "justifyMode": "auto", "textMode": "auto"}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def dashboard(uid: str, title: str, panels: list[dict]) -> dict:
    return {"uid": uid, "title": title, "schemaVersion": 39, "time": {"from": "now-30m", "to": "now"}, "timezone": "browser",
            "refresh": "5s", "tags": ["pd-gateway"], "graphTooltip": 1, "panels": panels}


def hq(metric: str, p: float, by: str = "", win: str = "1m") -> str:
    return f"histogram_quantile({p}, sum by (le{', ' + by if by else ''}) (rate({metric}_bucket[{win}])))"


def mean(metric: str, by: str = "pod", win: str = "1m") -> str:
    return f"sum by ({by}) (rate({metric}_sum[{win}])) / sum by ({by}) (rate({metric}_count[{win}]))"


SHED = "(sum(rate(gw_shed_total[1m])) or vector(0))"
ADMITTED = "(sum(rate(gw_admitted_total[1m])) or vector(0))"


# ---------------------------------------------------------------------------------- dashboards
def overview() -> dict:
    return dashboard("pdgw-overview", "PD Gateway / Overview", [
        stat(1, "Requests / s", "sum(rate(gw_requests_total[1m]))", 0, 0, "reqps"),
        stat(2, "Success ratio", 'sum(rate(gw_requests_total{code="200"}[1m])) / clamp_min(sum(rate(gw_requests_total[1m])), 1e-9)', 4, 0, "percentunit"),
        stat(3, "Shed / s", SHED, 8, 0, "reqps"),
        stat(4, "TTFT p50", hq("gw_ttft_seconds", 0.5), 12, 0, "s"),
        stat(5, "Output tokens / s", 'sum(rate(gw_tokens_total{kind="completion"}[1m]))', 16, 0),
        stat(6, "GPU util", f"DCGM_FI_DEV_GPU_UTIL{{{GPU}}}", 20, 0, "percent"),
        panel(7, "In flight per engine pod (gateway view)", [("gw_inflight", "{{role}} {{pod}}")], 0, 4),
        panel(8, "KV cache usage per pod", [("vllm:kv_cache_usage_perc", "{{role}} {{pod}}")], 12, 4, "percentunit"),
        panel(9, "Waiting in the gateway queue, by prompt class", [("sum by (cls) (gw_queue_depth)", "{{cls}}")], 0, 12),
        panel(10, "Waiting in vLLM per pod", [("vllm:num_requests_waiting", "{{role}} {{pod}}")], 12, 12),
        panel(11, "TTFT / E2E at the gateway (p50, p95)", [
            (hq("gw_ttft_seconds", 0.5), "TTFT p50"), (hq("gw_ttft_seconds", 0.95), "TTFT p95"),
            (hq("gw_e2e_seconds", 0.5), "E2E p50"), (hq("gw_e2e_seconds", 0.95), "E2E p95")], 0, 20, "s"),
        panel(12, "Tokens / s (prompt · cached · completion)", [("sum by (kind) (rate(gw_tokens_total[1m]))", "{{kind}}")], 12, 20),
    ])


def gateway() -> dict:
    return dashboard("pdgw-gateway", "PD Gateway / Gateway and queue", [
        stat(1, "Requests / s", "sum(rate(gw_requests_total[1m]))", 0, 0, "reqps", w=6),
        stat(2, "In flight (decode)", 'sum(gw_inflight{role="decode"})', 6, 0, w=6),
        stat(3, "Waiting in the queue", "sum(gw_queue_depth)", 12, 0, w=6),
        stat(4, "TTFT p95", hq("gw_ttft_seconds", 0.95), 18, 0, "s", w=6),
        panel(5, "Requests / s by HTTP code", [("sum by (code) (rate(gw_requests_total[1m]))", "{{code}}")], 0, 4, "reqps"),
        panel(6, "In flight per pod", [("gw_inflight", "{{role}} {{pod}}")], 12, 4),
        panel(7, "Queue depth by prompt class", [("sum by (cls) (gw_queue_depth)", "{{cls}}")], 0, 12,
              desc="two-class: short and long prompts wait apart and are served 9:1 while both wait"),
        panel(8, "Queue wait p50 / p95 by prompt class", [
            (hq("gw_queue_wait_seconds", 0.5, "cls"), "p50 {{cls}}"), (hq("gw_queue_wait_seconds", 0.95, "cls"), "p95 {{cls}}")], 12, 12, "s"),
        panel(9, "TTFT p50 / p90 / p99 (incl. queue wait)", [
            (hq("gw_ttft_seconds", 0.5), "p50"), (hq("gw_ttft_seconds", 0.9), "p90"), (hq("gw_ttft_seconds", 0.99), "p99")], 0, 20, "s"),
        panel(10, "Request E2E p50 / p95", [(hq("gw_e2e_seconds", 0.5), "p50"), (hq("gw_e2e_seconds", 0.95), "p95")], 12, 20, "s"),
        panel(11, "Gap between streamed chunks p50 / p99", [(hq("gw_itl_seconds", 0.5), "p50"), (hq("gw_itl_seconds", 0.99), "p99")], 0, 28, "s"),
        panel(12, "Tokens / s", [("sum by (kind) (rate(gw_tokens_total[1m]))", "{{kind}}")], 12, 28),
    ])


def admission() -> dict:
    """What admission lets in, what it turns away and why, what it sees - and whether the machine stays healthy while it sheds."""
    return dashboard("pdgw-admission", "PD Gateway / Admission (bucket · fleet · SLO)", [
        stat(1, "Levels on (of 3)", "sum(gw_admission_level) or vector(0)", 0, 0, w=3),
        stat(2, "SLO", "gw_admission_slo_seconds", 3, 0, "s", w=3),
        stat(3, "Admitted / s", ADMITTED, 6, 0, "reqps"),
        stat(4, "Shed / s", SHED, 10, 0, "reqps"),
        stat(5, "Shed share", f"{SHED} / clamp_min({SHED} + {ADMITTED}, 1e-9)", 14, 0, "percentunit"),
        stat(6, "Shed in the time range", "sum(increase(gw_shed_total[$__range])) or vector(0)", 18, 0, decimals=0, w=6),
        panel(7, "Admitted vs shed / s", [(ADMITTED, "admitted"), ("sum by (level) (rate(gw_shed_total[1m]))", "shed · {{level}}")], 0, 4, "reqps"),
        panel(8, "Shed so far, by level · reason · code", [("sum by (level, reason, code) (gw_shed_total)", "{{level}} · {{reason}} · {{code}}")], 12, 4,
              desc="bucket = 429 tenant_tokens; fleet = 503 kv_free / no_eligible_pod; slo = 429 deadline_unmeetable"),
        panel(9, "SLO check: estimated finish vs the SLO", [
            (hq("gw_slo_predicted_seconds", 0.5, "cls"), "p50 estimate · {{cls}}"),
            (hq("gw_slo_predicted_seconds", 0.9, "cls"), "p90 estimate · {{cls}}"),
            ("gw_admission_slo_seconds", "SLO")], 0, 12, "s",
              desc="queue_wait + n_in / prefill_tokens_per_s + n_out x ITL; above the SLO line -> 429 deadline_unmeetable"),
        panel(10, "Fleet check: KV usage per pod vs threshold", [("gw_fleet_kv_usage", "{{role}} {{pod}}"), ("gw_admission_kv_saturation", "threshold")],
              12, 12, "percentunit", desc="every pod of a pool at or above the threshold (or unreachable) -> 503 kv_free / no_eligible_pod"),
        panel(11, "SLO input: queue ahead and drain rate", [('gw_slo_input{name="vllm_waiting"}', "requests waiting in vLLM"),
                                                           ("sum(gw_queue_depth)", "requests waiting in the gateway"),
                                                           ('gw_slo_input{name="drain_per_s"}', "calls finished / s")], 0, 20),
        panel(12, "SLO input: expected output tokens (n_out)", [('gw_slo_input{name=~"n_out_.*"}', "{{name}}")], 12, 20),
        panel(13, "SLO input: prefill tokens / s (bounded)", [('gw_slo_input{name="prefill_tokens_per_s"}', "prefill tokens/s")], 0, 28),
        panel(14, "SLO input: inter-token latency", [('gw_slo_input{name="inter_token_latency_s"}', "ITL")], 12, 28, "s"),
        panel(15, "Fleet: pods eligible (1 = may admit)", [("gw_fleet_eligible", "{{role}} {{pod}}")], 0, 36),
        panel(16, "Token buckets: the 10 emptiest tenants", [("bottomk(10, gw_bucket_tokens)", "{{tenant}}")], 12, 36,
              desc="a request takes est. prompt + min(max_tokens, 1024); empty -> 429 tenant_tokens"),
        # machine health while shedding
        stat(17, "Running pods not ready", f'sum(kube_pod_status_ready{{{NS},condition="false"}} * on (namespace, pod) group_left '
                                           'kube_pod_status_phase{phase="Running"}) or vector(0)', 0, 44),
        stat(18, "Container restarts in the range", f"sum(increase(kube_pod_container_status_restarts_total{{{NS}}}[$__range])) or vector(0)", 4, 44, decimals=0),
        stat(19, "Upstream errors / s (non-200, not shed)",
             '(sum(rate(gw_requests_total{code!="200"}[1m])) or vector(0)) - (sum(rate(gw_shed_total[1m])) or vector(0))', 8, 44, "reqps"),
        stat(20, "GPU util", f"DCGM_FI_DEV_GPU_UTIL{{{GPU}}}", 12, 44, "percent"),
        stat(21, "GPU memory used", f"DCGM_FI_DEV_FB_USED{{{GPU}}} * 1024 * 1024", 16, 44, "bytes"),
        stat(22, "Node CPU", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))', 20, 44, "percentunit"),
    ])


def router() -> dict:
    routed = "sum by (role, pod) (rate(gw_routed_total[1m]))"
    return dashboard("pdgw-router", "PD Gateway / Router (prefix + bounded load)", [
        panel(1, "Requests routed / s per pod", [(routed, "{{role}} {{pod}}")], 0, 0),
        panel(2, "Share of the pool's requests per pod", [(f"{routed} / on (role) group_left sum by (role) (rate(gw_routed_total[1m]))", "{{role}} {{pod}}")],
              12, 0, "percentunit", desc="Balanced pools sit near 50% (2 pods per pool)."),
        panel(3, "In flight per pod", [("gw_inflight", "{{role}} {{pod}}")], 0, 8),
        panel(4, "In-flight imbalance per pool (max - min)", [("max by (role) (gw_inflight) - min by (role) (gw_inflight)", "{{role}}")], 12, 8,
              desc="Bounded load keeps a pod under ceil(mean x 1.25) in flight."),
        panel(5, "Router prefix guess for the chosen pod (p50 / p90)", [
            (hq("gw_prefix_match_ratio", 0.5, "role"), "p50 {{role}}"), (hq("gw_prefix_match_ratio", 0.9, "role"), "p90 {{role}}")], 0, 16, "percentunit"),
        panel(6, "Engine prefix-cache hit rate (what the pod really had)", [
            ("sum by (role, pod) (rate(vllm:prefix_cache_hits_total[1m])) / sum by (role, pod) (rate(vllm:prefix_cache_queries_total[1m]))", "{{role}} {{pod}}")],
              12, 16, "percentunit"),
    ])


def vllm() -> dict:
    return dashboard("pdgw-vllm", "PD Gateway / vLLM engines", [
        panel(1, "Running requests", [("vllm:num_requests_running", "{{role}} {{pod}}")], 0, 0),
        panel(2, "Waiting requests by reason (capacity = no KV space; deferred = waiting for remote KV)",
              [("sum by (role, pod, reason) (vllm:num_requests_waiting_by_reason)", "{{role}} {{pod}} {{reason}}")], 12, 0),
        panel(3, "KV cache usage", [("vllm:kv_cache_usage_perc", "{{role}} {{pod}}")], 0, 8, "percentunit"),
        panel(4, "Prefix cache hit rate", [
            ("sum by (role, pod) (rate(vllm:prefix_cache_hits_total[1m])) / sum by (role, pod) (rate(vllm:prefix_cache_queries_total[1m]))", "{{role}} {{pod}}")],
              12, 8, "percentunit"),
        panel(5, "Engine TTFT p50 / p95", [(hq("vllm:time_to_first_token_seconds", 0.5, "pod"), "p50 {{pod}}"),
                                           (hq("vllm:time_to_first_token_seconds", 0.95, "pod"), "p95 {{pod}}")], 0, 16, "s"),
        panel(6, "Inter-token latency p50 / p99", [(hq("vllm:inter_token_latency_seconds", 0.5, "pod"), "p50 {{pod}}"),
                                                   (hq("vllm:inter_token_latency_seconds", 0.99, "pod"), "p99 {{pod}}")], 12, 16, "s"),
        panel(7, "Queue time (mean)", [(mean("vllm:request_queue_time_seconds"), "{{pod}}")], 0, 24, "s", w=8),
        panel(8, "Prefill time (mean)", [(mean("vllm:request_prefill_time_seconds"), "{{pod}}")], 8, 24, "s", w=8),
        panel(9, "Decode time (mean)", [(mean("vllm:request_decode_time_seconds"), "{{pod}}")], 16, 24, "s", w=8),
        panel(10, "Prompt tokens / s by source", [("sum by (role, source) (rate(vllm:prompt_tokens_by_source_total[1m]))", "{{role}} {{source}}")], 0, 32),
        panel(11, "Generation tokens / s", [("sum by (role, pod) (rate(vllm:generation_tokens_total[1m]))", "{{role}} {{pod}}")], 12, 32),
        panel(12, "Request E2E p95 (engine)", [(hq("vllm:e2e_request_latency_seconds", 0.95, "pod"), "{{pod}}")], 0, 40, "s"),
        panel(13, "Preemptions / s", [("sum by (pod) (rate(vllm:num_preemptions_total[1m]))", "{{pod}}")], 12, 40),
    ])


def transfer() -> dict:
    return dashboard("pdgw-transfer", "PD Gateway / KV transfer (NIXL + Mooncake)", [
        panel(1, "NIXL bytes / s (prefill pushes to decode)", [("sum by (pod) (rate(vllm:nixl_bytes_transferred_sum[1m]))", "{{pod}}")], 0, 0, "Bps"),
        panel(2, "NIXL mean transfer time", [(mean("vllm:nixl_xfer_time_seconds"), "{{pod}}")], 12, 0, "s"),
        panel(3, "Prefill blocks released by lease expiry / s (vs pushes / s)", [
            ("sum by (pod) (rate(vllm:nixl_num_kv_expired_reqs_total[1m]))", "expired {{pod}}"),
            ("sum by (pod) (rate(vllm:nixl_xfer_time_seconds_count[1m]))", "pushes {{pod}}")], 0, 8,
              desc="Should stay ~0 with vLLM >= 0.31: a push frees its blocks when the WRITE completes."),
        panel(4, "KV cache usage: prefill vs decode", [("vllm:kv_cache_usage_perc", "{{role}} {{pod}}")], 12, 8, "percentunit"),
        panel(5, "Mooncake store ops / s", [("sum by (operation, status) (rate(vllm:mooncake_store_operation_total[1m]))", "{{operation}} {{status}}")], 0, 16),
        panel(6, "Mooncake op time (mean)", [(mean("vllm:mooncake_store_operation_time_seconds", "operation"), "{{operation}}")], 12, 16, "s"),
        panel(7, "Mooncake bytes / s", [("sum by (operation) (rate(vllm:mooncake_store_operation_bytes_total[1m]))", "{{operation}}")], 0, 24, "Bps"),
        panel(8, "External (Mooncake) prefix hit rate", [
            ("sum(rate(vllm:external_prefix_cache_hits_total[1m])) / sum(rate(vllm:external_prefix_cache_queries_total[1m]))", "hit rate")], 12, 24, "percentunit"),
        panel(9, "Mooncake master: keys", [("master_key_count", "keys"), ("master_evicted_key_count", "evicted keys")], 0, 32),
        panel(10, "Mooncake master: allocated / capacity", [("master_allocated_bytes", "allocated"), ("master_total_capacity_bytes", "capacity")], 12, 32, "bytes"),
    ])


def hami() -> dict:
    return dashboard("pdgw-hami", "PD Gateway / HAMi GPU slices", [
        panel(1, "Slice memory used per pod", [(f"hami_vgpu_memory_used_bytes{{{NS}}}", "{{pod}}"),
                                               (f"max(hami_vgpu_memory_limit_bytes{{{NS}}})", "slice limit")], 0, 0, "bytes"),
        panel(2, "Slice memory used / limit", [(f"hami_vgpu_memory_used_bytes{{{NS}}} / hami_vgpu_memory_limit_bytes{{{NS}}}", "{{pod}}")], 12, 0, "percentunit"),
        # HAMi *_ratio metrics are 0-100
        panel(3, "GPU utilisation per slice", [(f"hami_container_device_utilization_ratio{{{NS}}}", "{{pod}}")], 0, 8, "percent"),
        panel(4, "Host GPU utilisation", [("hami_host_gpu_utilization_ratio", "GPU {{device_index}}")], 12, 8, "percent"),
        panel(5, "Allocated vs limit per GPU (memory)", [("hami_gpu_memory_allocated_bytes", "allocated GPU {{device_index}}"),
                                                         ("hami_gpu_memory_limit_bytes", "limit GPU {{device_index}}")], 0, 16, "bytes"),
        panel(6, "Slices on each GPU", [("hami_gpu_shared_count", "GPU {{device_index}}")], 12, 16),
    ])


def cluster() -> dict:
    return dashboard("pdgw-cluster", "PD Gateway / Cluster and GPU (DCGM)", [
        stat(1, "Node ready", 'sum(kube_node_status_condition{condition="Ready",status="true"})', 0, 0),
        stat(2, "Node CPU", '1 - avg(rate(node_cpu_seconds_total{mode="idle"}[1m]))', 4, 0, "percentunit"),
        stat(3, "Node memory available", "sum(node_memory_MemAvailable_bytes)", 8, 0, "bytes"),
        stat(4, "Pods running (all ns)", 'sum(kube_pod_status_phase{phase="Running"})', 12, 0),
        stat(5, "GPU power", f"DCGM_FI_DEV_POWER_USAGE{{{GPU}}}", 16, 0, "watt"),
        stat(6, "GPU memory used", f"DCGM_FI_DEV_FB_USED{{{GPU}}} * 1024 * 1024", 20, 0, "bytes"),
        panel(7, "GPU utilisation", [("DCGM_FI_DEV_GPU_UTIL", "GPU {{gpu}}")], 0, 4, "percent"),
        panel(8, "Tensor pipe / DRAM active", [("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "tensor GPU {{gpu}}"),
                                                ("DCGM_FI_PROF_DRAM_ACTIVE", "DRAM GPU {{gpu}}")], 12, 4, "percentunit"),
        panel(9, "Framebuffer used", [("DCGM_FI_DEV_FB_USED * 1024 * 1024", "GPU {{gpu}}")], 0, 12, "bytes"),
        panel(10, "Power / temperature", [("DCGM_FI_DEV_POWER_USAGE", "W GPU {{gpu}}"), ("DCGM_FI_DEV_GPU_TEMP", "°C GPU {{gpu}}")], 12, 12),
        panel(11, "Node CPU by mode", [('sum by (mode) (rate(node_cpu_seconds_total{mode!="idle"}[1m]))', "{{mode}}")], 0, 20),
        panel(12, "Node memory used", [("sum(node_memory_MemTotal_bytes) - sum(node_memory_MemAvailable_bytes)", "used")], 12, 20, "bytes"),
        panel(13, "Pods by phase (all namespaces)", [("sum by (phase) (kube_pod_status_phase)", "{{phase}}")], 0, 28),
        panel(14, "Restarts (any namespace, > 0)", [("kube_pod_container_status_restarts_total > 0", "{{namespace}}/{{pod}}")], 12, 28),
        panel(15, "Container CPU (cores)", [(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS},container!="",container!="POD"}}[1m]))', "{{pod}}")], 0, 36),
        panel(16, "Container memory (working set)", [(f'sum by (pod) (container_memory_working_set_bytes{{{NS},container!="",container!="POD"}})', "{{pod}}")],
              12, 36, "bytes"),
    ])


def errors() -> dict:
    total = "sum(rate(gw_requests_total[1m]))"
    return dashboard("pdgw-errors", "PD Gateway / Success and failures", [
        stat(1, "Requests / s", total, 0, 0, "reqps"),
        stat(2, "Success / s", 'sum(rate(gw_requests_total{code="200"}[1m]))', 4, 0, "reqps"),
        stat(3, "Non-200 / s", 'sum(rate(gw_requests_total{code!="200"}[1m])) or vector(0)', 8, 0, "reqps"),
        stat(4, "Success ratio", f'sum(rate(gw_requests_total{{code="200"}}[1m])) / clamp_min({total}, 1e-9)', 12, 0, "percentunit"),
        stat(5, "NIXL failed transfers", "sum(vllm:nixl_num_failed_transfers_total) or vector(0)", 16, 0),
        stat(6, "Mooncake failed ops", 'sum(vllm:mooncake_store_operation_total{status!="ok"}) or vector(0)', 20, 0),
        panel(7, "Requests / s by HTTP code", [("sum by (code) (rate(gw_requests_total[1m]))", "{{code}}")], 0, 4),
        panel(8, "Non-200 share (shed + upstream errors)", [(f'(sum(rate(gw_requests_total{{code!="200"}}[1m])) or vector(0)) / clamp_min({total}, 1e-9)', "non-200 / requests")],
              12, 4, "percentunit"),
        panel(9, "vLLM finished requests / s by reason", [("sum by (finished_reason) (rate(vllm:request_success_total[1m]))", "{{finished_reason}}")], 0, 12,
              desc="length = hit max_tokens; abort / error = failed"),
        panel(10, "KV transfer failures", [("sum by (pod) (rate(vllm:nixl_num_failed_transfers_total[1m]))", "NIXL {{pod}}"),
                                           ("sum by (pod) (rate(vllm:nixl_num_failed_notifications_total[1m]))", "NIXL notif {{pod}}"),
                                           ('sum by (operation, status) (rate(vllm:mooncake_store_operation_total{status!="ok"}[1m]))', "Mooncake {{operation}} {{status}}")],
              12, 12),
    ])


DASHBOARDS = {"overview": overview, "gateway": gateway, "admission": admission, "router": router, "vllm": vllm,
              "transfer": transfer, "hami": hami, "cluster": cluster, "errors": errors}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, f in DASHBOARDS.items():
        (OUT / f"pdgw-{name}.json").write_text(json.dumps(f(), indent=1), encoding="utf-8", newline="\n")
        print(f"wrote {OUT / f'pdgw-{name}.json'}")


if __name__ == "__main__":
    main()
