# Operational scripts

Interactive and diagnostic tooling for a running open-serve deployment. All
scripts assume `kubectl` access to the cluster (except the pure-HTTP ones,
which only need the gateway URL and an API key).

| Script | Purpose |
|---|---|
| `gcp-preflight.sh` | Prepare/verify a GCP project for deployment: checks gcloud auth, project access, and billing; enables all required Google APIs; checks ADC; reports GPU/CPU/SSD quotas with request instructions for anything at zero. Safe to re-run. |
| `validate-catalog.sh` | Render every catalog preset (and an all-enabled combined pass) through `helm template`; run after adding or editing a preset. |
| `connect.sh` | Port-forward the gateway, Ray dashboard, Grafana, and Prometheus, then chat with (or embed against) a selected model interactively. |
| `test-endpoints.sh` | End-to-end smoke test of every gateway endpoint: models list, chat (plain/streaming/tools/vision), embeddings, `/v1/responses`, tokenize/detokenize roundtrip, auth enforcement, error handling. |
| `load-test.sh` | Sustained concurrent chat/embedding load against models to measure autoscaling and node-provisioning behavior; logs scaling events and latency percentiles. |
| `load-test-embeddings.py` | Embedding-endpoint throughput ceiling finder: concurrency sweeps, batch sizing, p50/p95/p99 latency, optional per-stage (tokenize/embed/detokenize) breakdown. |
| `test-scaleup.py` | Measure Ray Serve scale-up time phase-by-phase (autoscaler decision → schedule → image pull → model load → Ready), with auto-discovery of models from the live HelmRelease values and CI-friendly JSON output. |
| `test-status.sh` | E2E test of the status page: `/healthz`, `/status.json` contract invariants, HTML render, and an optional incident-injection mode (green → down → green). |

> Model pre-staging to object storage is not covered here yet — a
> Kubernetes Job replacement for the old VM-based staging flow is on the
> roadmap.

## Environment variables

### `connect.sh`
| Var | Meaning | Default |
|---|---|---|
| `API_KEY` | bearer token (or `--api-key`) | required |
| `NAMESPACE` | Kubernetes namespace | `open-serve` |
| `CLUSTER` | GKE cluster name (or `--cluster`) | current kube context |
| `GCP_PROJECT` / `GCP_REGION` | needed with `--cluster` for `gcloud container clusters get-credentials` | — |
| `GATEWAY_SVC` | gateway Service name | `open-serve-gateway` |

### `test-endpoints.sh`
| Var | Meaning | Default |
|---|---|---|
| `BASE_URL` | gateway base URL (or first positional arg) | required |
| `API_KEY` | bearer token (or `--api-key`) | required |
| `CHAT_MODELS` | space-separated chat models | `qwen3-8b` |
| `EMBED_MODELS` | embedding models | none (section skipped) |
| `VL_MODELS` | vision-language models | none (section skipped) |
| `RESPONSES_MODELS` | vllm-raw models for `/v1/responses` | `CHAT_MODELS` |
| `TOKENIZE_MODELS` | models for `/tokenize` + `/detokenize` | `CHAT_MODELS` |

### `load-test.sh`
| Var | Meaning | Default |
|---|---|---|
| `BASE_URL` | endpoint base URL (or `--url`) | `http://127.0.0.1:8000` |
| `NAMESPACE` | Kubernetes namespace (for scaling-event monitoring) | `open-serve` |
| `MODELS` | space-separated chat models (or `--model`) | `qwen3-8b` |
| `EMBED_MODELS` | embedding models (or `--embed-model`) | none |

### `load-test-embeddings.py`
| Var | Meaning | Default |
|---|---|---|
| `BASE_URL` | gateway base URL (or `--api-url`) | required |
| `API_KEY` | bearer token (or `--api-key` / `--api-key-file`) | required |
| `NAMESPACE` | namespace for `--context` in-cluster diagnostics | `open-serve` |

### `test-scaleup.py`
| Var | Meaning | Default |
|---|---|---|
| `BASE_URL` | gateway base URL (or `--api-url`) | required |
| `API_KEY` | bearer token (or `--api-key` / `--api-key-from-secret`) | required |
| `KUBE_CONTEXT` | kubectl context (or `--context`) | current kube context |
| `NAMESPACE` | Kubernetes namespace | `open-serve` |

Requires PyYAML (`pip install PyYAML`).

### `test-status.sh`
| Var | Meaning | Default |
|---|---|---|
| `KUBE_CONTEXT` | kubectl context (or `--context`) | current kube context |
| `NAMESPACE` | Kubernetes namespace (or `--namespace`) | `open-serve` |
| `STATUS_SVC` | status-page Service name | `open-serve-status` |
| `LOCAL_PORT` | local port for the port-forward (or `--port`) | `18000` |
| `INJECT_MODEL` | model for `--inject-incident` | `qwen3-8b` |
