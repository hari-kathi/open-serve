# Observability

open-serve ships observability as chart resources that plug into an existing **kube-prometheus-stack** (the Flux reference layout installs one): PodMonitors for scraping, six Grafana dashboards, Prometheus alert rules, a synthetic probe, and a public status page. Master switch: `monitoring.enabled` (dashboards additionally behind `monitoring.dashboards.enabled`).

## Grafana dashboards

Six dashboards ship as a ConfigMap labeled `grafana_dashboard: "1"`, auto-discovered by the Grafana sidecar:

| Dashboard | What it shows |
|---|---|
| **Default Dashboard** | Ray cluster fundamentals — nodes, CPU/GPU/memory, object store |
| **Serve Dashboard** | Ray Serve cluster-wide: QPS, error rate, latency percentiles across applications |
| **Serve Deployment Dashboard** | Per-deployment drill-down: replicas, per-replica QPS and error QPS, queue depths |
| **Serve LLM Dashboard** | vLLM engine internals: TTFT, TPOT, KV-cache usage, running/waiting requests |
| **Ray Serve - Model Comparison** | Side-by-side across models: P95 TTFT / TPOT / E2E latency, tokens/sec, requests/sec, prefix-cache hit rate, healthy replicas, GPU utilization and memory |
| **Ray Serve - SLO Tracking** | SLO view built on the recording rules (`ray_vllm:ttft_p95:5m`, `ray_vllm:tpot_p95:5m`, throughput, prefix-cache hit rate) |

## Alerts

`PrometheusRule` groups, with their gates and tunables (all thresholds under `monitoring.alerts.*` in values):

| Group | Rules | Gate |
|---|---|---|
| `ray-serve-latency` | `RayServeHighLatency` (P95 processing latency > `latency.rayServeP95Ms`, default 10s) | always on |
| `ray-serve-availability` | `RayServeNoReplicas` (warning, 2m) and `RayServeNoReplicasCritical` (critical, 10m) — **gated on `tier="production"`** since internal-test models scale to zero by design; `RayServeHighErrorRate` (> `availability.errorRateThreshold`, default 5%, un-gated); `RayServeReplicaRestarts` (> `availability.replicaRestartsThreshold` in 10m); `OpenServeWorkerPending` (RayCluster-owned pod Pending >3m — tuned to fire *before* KubeRay deletes unschedulable GPU pods) | always on |
| `ray-serve-saturation` | `RayServeHighQueuedQueries`, `RayServeHighQueueDepth` (thresholds under `saturation.*`) | always on |
| `ray-serve-gpu` | `GPUMemoryHigh` (warning, default 95%), `GPUMemoryCritical` (critical, default 98%) | always on |
| `ray-serve-vllm-latency` | `VLLMHighTTFT`, `VLLMHighTPOT` (warnings), `VLLMHighE2ELatency` (critical) — all **gated on `tier="production"`** so cold starts of scale-to-zero test models don't page | `monitoring.alerts.vllm.enabled` (disable if no vLLM models) |
| `ray-serve-vllm-slo` | Recording rules only: `ray_vllm:ttft_p95:5m`, `ray_vllm:tpot_p95:5m`, `ray_vllm:throughput_tokens_per_sec:5m`, `ray_vllm:prefix_cache_hit_rate:5m` | `monitoring.alerts.vllm.enabled` |
| `ray-serve-probes` | `ModelProbeFailed` (warning, 5m), `ModelProbeDownCritical` (critical, success <50% over 10m), one `ModelProbeHighLatency` per entry in `monitoring.alerts.probes.slo.perModel`, `ExternalGatewayBroken` (critical: external probe failing while internal healthy — points at Gateway/TLS/gateway Deployment, not the model) | `monitoring.alerts.probes.enabled` (defaults to `probe.enabled`) |
| `ray-serve-tier-info` | `InternalTestModelColdStart` (severity `info`, no paging — someone is exercising an internal-test model) | always on |

Models without a `probes.slo.perModel` entry get **no** latency alert — add one when promoting a model to production (embedding models are near-instant, e.g. `1`; LLMs scale with size, e.g. `10`).

## Synthetic probe

The probe (`services/probe`, enabled via `probe.enabled`) is a long-running Deployment with an internal scheduler — default cadence **15 minutes** (`probe.intervalSeconds: 900`). It probes **only `tier: production` models**; internal-test models are never probed. Each cycle, per target, two paths:

- **Internal liveness** — direct to the model's RayService Service, bypassing the gateway. Runner-aware: `chat`/`embedding` runners get `GET /v1/models` with an assertion that the `modelId` is registered; any other runner falls back to Ray Serve's universal `GET /-/healthy`.
- **External functional** — through the gateway (the real customer path: LB + TLS + auth + routing + worker). `chat` → `POST /v1/chat/completions` (asserts `choices`); `embedding` → `POST /v1/embeddings` (asserts an embedding); other runners are skipped until a tailored handler is added (the extension contract is documented in `services/probe/main.py`).

The external probe authenticates with the key-map entry whose source is `probe.authSourceName` (default `monitor` — see [API keys](api-keys.md#the-probes-key-authsourcename)). Results land in `openserve_probe_success`, `openserve_probe_latency_seconds`, `openserve_probe_errors_total`, and `openserve_probe_last_success_timestamp`, all labeled `{model, endpoint, path, tier}`. The internal/external split is what makes `ExternalGatewayBroken` possible: external failing + internal healthy isolates the fault to the edge.

## Status page

The status page (`services/status`, enabled via `statusPage.enabled`) is a read-only FastAPI service that renders the probe metrics from Prometheus as a public three-tier status page (in the style of status.openai.com): one row per model with expandable internal/external sub-rows, an hourly uptime strip, and a recent-incident history. It's ClusterIP-only — expose it through the gateway, which forwards `/status`, `/status.json`, and `/static/` without auth.

Configuration (chart values → env vars):

| Value | Env var | Default | Meaning |
|---|---|---|---|
| `statusPage.prometheusUrl` | `PROMETHEUS_URL` | kube-prometheus-stack Service in `open-serve` | In-cluster Prometheus to query |
| `statusPage.cacheTtlSeconds` | `CACHE_TTL_SECONDS` | 30 | Snapshot cache TTL (bounds Prometheus load under page traffic) |
| `statusPage.stripDays` | `STRIP_DAYS` | 14 | Uptime strip window; one bar per UTC hour |
| `statusPage.recentHistoryDays` | `RECENT_HISTORY_DAYS` | 30 | Window for daily incident cards |
| `statusPage.incidentMinConsecutiveSamples` | `INCIDENT_MIN_CONSECUTIVE_SAMPLES` | 2 | Blip filter (below) |
| `statusPage.probeIntervalSeconds` | `PROBE_INTERVAL_S` | 900 | Must match `probe.intervalSeconds` |
| `statusPage.defaultLatencySloS` | `DEFAULT_LATENCY_SLO_S` | 10 | Latency SLO when a model has no `perModel` entry |
| `statusPage.categoryOrder` / `branding` | config files + `BRANDING_*` | — | Section order and page branding |

Per-model metadata (`category`, `description`) comes from the `serveModels` entries; per-model SLOs come from `monitoring.alerts.probes.slo.perModel` — one configuration feeding alerts and the page consistently.

**Classification** is SLO- and latency-aware. Per (model, endpoint), from the 5-minute probe success rate and P95 probe latency:

- no data → **unknown** · success < 50% → **down** · success < 99% → **degraded**
- success healthy but P95 latency above the model's SLO → **degraded** ("up but slow" — this promotion never masks a down state)
- otherwise → **healthy**

A model row shows the worst state across its endpoints — internal failures are surfaced as user-facing on purpose, since most consumers are in-cluster.

**Blip filter:** an incident requires ≥ `incidentMinConsecutiveSamples` (default 2) consecutive failed probe samples. This filters the single-sample blips typical of KubeRay zero-downtime swaps; the flip side is that at 15-minute cadence a real outage takes ≥15 minutes to surface as an incident. The hourly strip applies the same rule, so the strip and the incident list never disagree. When Prometheus is briefly unreachable the page serves its last snapshot (marked stale) rather than erroring.

Test a deployment end-to-end with `scripts/test-status.sh` (contract checks on `/status.json`, HTML render, optional incident-injection mode).

## Accessing Grafana and Prometheus

Both run in-cluster (no public exposure by default). Port-forward on demand:

```bash
# Grafana (kube-prometheus-stack release in the open-serve namespace)
kubectl -n open-serve port-forward svc/kube-prometheus-stack-grafana 3000:80
# → http://localhost:3000

# Prometheus
kubectl -n open-serve port-forward svc/kube-prometheus-stack-prometheus 9090:9090
# → http://localhost:9090
```

(Adjust Service names if your monitoring stack uses a different release name or namespace — the defaults above match the Flux reference layout.) `scripts/connect.sh` sets up all of these port-forwards, plus the gateway and Ray dashboard, in one go.
