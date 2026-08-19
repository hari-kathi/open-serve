# End-to-end on GCP

Go from a fresh GCP project to a GPU-backed, OpenAI-compatible serving endpoint. The path has two layers:

1. **Infrastructure** — reference Terraform for GKE + GPU node pools (`examples/gcp-quickstart/`, backed by the modules in `terraform/gcp/`).
2. **Software** — the FluxCD GitOps layout in `deploy/flux/` that installs KubeRay, monitoring, and the open-serve chart.

!!! warning "Validation status"
    The open-serve stack itself is validated on kind locally (see the [kind quickstart](kind.md)). The GCP end-to-end path — reference Terraform plus Flux bootstrap on GKE — is **under active validation** ahead of v0.1.0; expect rough edges and check `examples/gcp-quickstart/` for the current state.

**Bring-your-own-cluster is a first-class path.** If you already have a GKE (or any Kubernetes) cluster with GPU nodes and the Prometheus Operator CRDs available, skip straight to [the software layer](#2-software-layer-fluxcd).

## Prerequisites

- A GCP account with billing enabled, and `gcloud` authenticated
- OpenTofu or Terraform >= 1.5
- `kubectl`, `helm`, and the [Flux CLI](https://fluxcd.io/flux/installation/)
- A GitHub (or other Git) repo to act as your GitOps repo

## 0. Preflight

Run the preflight script against your project before anything else. It verifies gcloud auth, project access, and billing; **enables all required Google APIs**; checks for Application Default Credentials; and reports your GPU/CPU/SSD quotas with instructions for anything at zero:

```bash
scripts/gcp-preflight.sh <PROJECT_ID> [REGION]   # region defaults to us-central1
```

It is safe to re-run at any time and exits non-zero while action items remain. Two items it cannot fix for you:

- **Credentials for Terraform** — either `gcloud auth application-default login` (persistent), or a short-lived token per shell: `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)` (expires after ~1 hour; re-export and re-run apply if a long apply outlives it).
- **GPU quota** — see below.

!!! warning "GPU quota"
    New GCP projects typically have **zero GPU quota**. Request quota for your chosen accelerator (e.g. `NVIDIA_A100_GPUS` or `NVIDIA_L4_GPUS`) in your target region *before* applying the cluster layer — quota requests can take hours to days, and without quota GPU node pools scale up to nothing (pods stay `Pending`, then the OpenServeWorkerPending alert fires).

## 1. Infrastructure layer (Terraform)

The reference root in `examples/gcp-quickstart/` wires three modules (from `terraform/gcp/`) into one apply:

| Module | What it creates |
|---|---|
| `bootstrap` | Enabled APIs on the existing project; optional state + model-weights buckets |
| `network` | VPC, GKE-ready subnet with pods/services secondary ranges, Cloud NAT |
| `cluster` | The GKE cluster plus GPU node pools (scale-to-zero, spot, data-driven pool map) and the Workload Identity worker service account |

```bash
cd examples/gcp-quickstart
cp terraform.tfvars.example terraform.tfvars   # edit project_id
tofu init && tofu apply                        # or terraform
```

See `terraform/gcp/README.md` for module inputs (accelerator SKUs, pool sizing, spot). Once the cluster exists:

```bash
gcloud container clusters get-credentials <cluster-name> --region <region> --project <project>
kubectl get nodes
```

## 2. Software layer (FluxCD)

The reference layout in `deploy/flux/` uses the classic kustomize **base + environment overlay** pattern. Copy `environments/example` and `clusters/example` into your GitOps repo per real environment (`dev`, `prod`, ...) and adapt the overlay `values.yaml` files.

Flux reconciles four components as a dependency DAG:

```
configs ──┬─▶ kuberay-operator ──────────┬─▶ open-serve
          └─▶ prometheus-operator-crds ─▶ monitoring ─┘
```

- **configs** — the `open-serve` namespace and the `open-serve-worker` ServiceAccount (used by Ray pods, e.g. for bucket access to model weights via Workload Identity)
- **kuberay-operator** — upstream KubeRay chart; must exist before RayService CRs can reconcile
- **prometheus-operator-crds** + **monitoring** — kube-prometheus-stack; the open-serve chart ships PodMonitors and PrometheusRules that need the CRDs
- **open-serve** — the serving chart itself, pulled from OCI (`oci://ghcr.io/hari-kathi/charts/open-serve`)

### Bootstrap Flux

```bash
flux bootstrap github \
  --owner=<you> \
  --repository=<your-gitops-repo> \
  --path=deploy/flux/clusters/example
```

From then on, **merging to the tracked branch is the deployment mechanism**. Validate overlays locally before pushing:

```bash
kubectl kustomize deploy/flux/environments/example/open-serve
```

### Create the API-key Secret

The gateway authenticates against a key map in the `open-serve-api-keys` Secret, which is deliberately **not** in Git. Create it once per cluster:

```bash
kubectl -n open-serve create secret generic open-serve-api-keys \
  --from-literal=key-map.json='{"sk-team1-<hex>": "team1", "sk-monitor-<hex>": "monitor"}'
```

The `monitor` entry is the key the synthetic probe uses for its external functional probes. Full format, rotation, and conventions: [API keys](../operations/api-keys.md).

### Enable a model

In your environment overlay's `open-serve/values.yaml`, merge a catalog preset (from `catalog/models/`) under `serveModels:` and set `enabled: true`, then wire the gateway route. Worked example: [Adding a model](../operations/adding-a-model.md).

```yaml
gateway:
  enabled: true

serveModels:
  qwen3-8b:
    enabled: true
    # ... preset contents from catalog/models/ ...
```

Commit and push; Flux upgrades the HelmRelease, KubeRay creates `rayservice-qwen3-8b`, and the cluster autoscaler provisions a GPU node (first spin-up includes image pull + model load — tens of minutes is normal for large images).

### Validate

```bash
kubectl -n open-serve get rayservice,pods
kubectl -n open-serve port-forward svc/open-serve-gateway 8000:8000

BASE_URL=http://localhost:8000 API_KEY=sk-team1-<hex> \
  CHAT_MODELS="qwen3-8b" ./scripts/test-endpoints.sh
```

## 3. Optional: external HTTPS endpoint

To expose the gateway publicly, enable `externalGateway` in the chart values. It renders a Kubernetes Gateway API `Gateway` + `HTTPRoute`; the default class targets GKE's global external managed load balancer, but any Gateway API implementation works (Istio, Envoy Gateway, NGINX Gateway Fabric — set `externalGateway.className`).

```yaml
externalGateway:
  enabled: true                 # requires gateway.enabled=true
  className: "gke-l7-global-external-managed"
  hostname: "models.example.com"
  gcpBackendPolicy:
    enabled: true               # GKE only: raises the 30s backend timeout that
    timeoutSec: 600             # otherwise cuts off long streaming responses
```

## Next steps

- [Adding a model](../operations/adding-a-model.md) — the full runbook
- [Observability](../operations/observability.md) — dashboards, alerts, probe, status page
- [Cost](../operations/cost.md) — scale-to-zero, autoscaling caps, spot pools
