# open-serve on kind — local end-to-end example

Proves the whole open-serve stack on a laptop, no GPUs and no cloud account:

1. Builds the three service images (`gateway`, `probe`, `status`) from
   `services/` — validating the Dockerfiles — and deploys the **gateway**.
2. Creates a single-node [kind](https://kind.sigs.k8s.io/) cluster
   (`kind.yaml`) and installs the upstream **KubeRay operator**.
3. Installs the **open-serve chart** (`values.yaml`) with one CPU-only
   `type: custom` model named `echo`: an inline `serveScript` (a tiny Ray
   Serve app with a FastAPI ingress) that speaks a minimal OpenAI surface —
   `GET /v1/models` and `POST /v1/chat/completions` (echoes the last user
   message back as a canned completion).
4. Asserts through the gateway that **auth is enforced** and **routing
   works**:
   - `GET /v1/models` without a key → `401`
   - `GET /v1/models` with the key → `200`, listing model id `echo`
   - `POST /v1/chat/completions` (`model: echo`) → `200` canned completion
     containing `echo: <your message>`
   - `GET /healthz` → `200` without auth (gateway checks the backend itself)

The API key is a throwaway `sk-test-<random hex>` generated at runtime and
stored only in the in-cluster `open-serve-api-keys` Secret (re-runs reuse it).

## Prerequisites

- Docker (daemon running; ~6 GB free disk for images)
- `kind`, `kubectl`, `helm` (v3+; tested with Helm 4)
- Works on Apple Silicon out of the box: the chart values pin the arm64 Ray
  image `rayproject/ray:2.53.0-py311-aarch64`; `run.sh` switches to the
  amd64 tag automatically on x86 hosts. (The service images themselves are
  built `linux/amd64` by their Dockerfiles; on Apple Silicon they run in
  kind via Docker Desktop's Rosetta emulation.)

## Run

```bash
examples/kind-local/run.sh      # idempotent — safe to re-run
examples/kind-local/cleanup.sh  # deletes the kind cluster
```

First run takes a few minutes (pulls the ~1 GB Ray image and loads it into
the kind node). Expected tail of the output:

```
Running e2e assertions
PASS: GET /v1/models without auth returns 401
PASS: GET /v1/models with auth lists model 'echo'
PASS: POST /v1/chat/completions echoes the user message
PASS: GET /healthz returns 200 without auth

===============================================
 e2e result: 4 passed, 0 failed
 API key (local throwaway): sk-test-…
 Gateway:  kubectl port-forward -n open-serve svc/open-serve-gateway 18080:8000
 Cleanup:  examples/kind-local/cleanup.sh
===============================================
```

## Poking around

```bash
# Talk to the gateway
kubectl port-forward -n open-serve svc/open-serve-gateway 18080:8000 &
API_KEY=$(kubectl get secret open-serve-api-keys -n open-serve \
  -o jsonpath='{.data.key-map\.json}' | base64 -d | sed -E 's/.*"(sk-test-[0-9a-f]+)".*/\1/')
curl -s -H "Authorization: Bearer $API_KEY" localhost:18080/v1/models
curl -s -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"echo","messages":[{"role":"user","content":"hi"}]}' \
  localhost:18080/v1/chat/completions

# Ray dashboard (Serve apps, actors, logs)
kubectl port-forward -n open-serve svc/rayservice-echo-head-svc 8265:8265 &
open http://localhost:8265

# Gateway usage metrics (Prometheus text format)
curl -s localhost:18080/metrics | grep openserve_

# Watch KubeRay reconcile
kubectl get rayservice,raycluster,pods -n open-serve -w
```

## What's deliberately turned off

`probe`, `statusPage`, `externalGateway`, and `monitoring` are disabled in
`values.yaml` so the chart installs on a bare kind cluster (no
kube-prometheus-stack or Gateway API CRDs required). With
`monitoring.enabled=false` the chart renders zero
PodMonitor/PrometheusRule/Grafana resources.

## Files

- `kind.yaml` — single-node kind cluster config (cluster name `open-serve`)
- `values.yaml` — chart values: arm64 Ray image, gateway wired to the `echo`
  RayService via `modelRoutes`, inline `serveScript`
- `run.sh` — build → cluster → operator → chart → wait → assertions
- `cleanup.sh` — `kind delete cluster --name open-serve`
