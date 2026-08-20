# End-to-end on GCP

Go from a GCP project to a GPU-backed, OpenAI-compatible serving endpoint on GKE. Every command below was executed against a real, freshly created project; the traps called out are ones we actually hit.

**What you'll build:** a regional GKE cluster with a CPU system pool and scale-to-zero GPU pools; KubeRay; the open-serve chart (gateway with API-key auth + per-model RayServices); and your first model from the catalog.

**Audience:** basic familiarity with Kubernetes, Terraform, and GCP. No Ray or vLLM knowledge needed.

!!! info "Validation status"
    Infrastructure, gateway auth/routing, and CPU-model serving are validated end-to-end on a fresh GCP project. GPU serving is validated up to node scheduling; the final hop depends only on your project's GPU quota (see step 2).

**Bring-your-own-cluster is first-class.** Already have GKE with GPU nodes? Skip to [step 4](#4-software-layer).

## 0. Tools

- `gcloud` (authenticated: `gcloud auth login`), `kubectl`, `helm` ≥ 3.9
- [OpenTofu](https://opentofu.org/) or Terraform ≥ 1.5 (commands below say `tofu`; `terraform` is interchangeable)

Optional but tidy — give this project its own gcloud configuration:

```bash
gcloud config configurations create open-serve
gcloud config set project <PROJECT_ID>
gcloud config set compute/region us-central1
```

## 1. Preflight

```bash
scripts/gcp-preflight.sh <PROJECT_ID> us-central1
```

This verifies gcloud auth, project access, and billing; **enables all required Google APIs** (a fresh project is missing ~7 of them); checks Application Default Credentials; and reports every quota that matters. Re-run it until the summary is clean — it exits non-zero while action items remain.

Two things it cannot do for you:

**Credentials for Terraform.** Either of:

```bash
gcloud auth application-default login          # persistent (browser)
# — or, non-interactive, valid ~1 hour:
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
```

**Billing tier.** A free-tier billing account **cannot create GPU instances at all**, regardless of quota — instance creation fails with *"billing account is currently in the free tier where non-TPU accelerators are not available"*. Upgrade at Console → Billing → Upgrade before requesting quota.

## 2. GPU quota — start this now

Two separate gates, both zero on new projects:

| Quota | Scope | Why it matters |
|---|---|---|
| **GPUs (all regions)** (`GPUS_ALL_REGIONS`) | project-global | Screens **every** GPU VM — spot included. Invisible in regional quota listings. |
| `NVIDIA L4 GPUs` / `Preemptible NVIDIA L4 GPUs` | per region | The per-family quota for the pool type you use |

Request "GPUs (all regions)" = 1–2 and the L4 quota for your region at **IAM & Admin → Quotas**. Expect friction on young accounts: the API-side request (`gcloud quotas preferences create ...`, printed by preflight) is often auto-denied in seconds; console requests can be partially approved or denied, then succeed after a day or two of paid billing history. The rest of this guide works fine while you wait.

## 3. Infrastructure

```bash
cd examples/gcp-quickstart
cp terraform.tfvars.example terraform.tfvars   # set project_id
tofu init && tofu apply
```

~15–20 minutes. You get: VPC + NAT, a regional GKE cluster (Workload Identity, Gateway API enabled), a `system` pool (e2-standard-4, 1–3), a spot L4 pool and an A100 pool (both scale-to-zero), the `open-serve-worker` service account with Workload Identity binding, and — important on new projects — an `artifactregistry.reader` grant for the node service account (without it, kubelet gets 403 pulling from same-project Artifact Registry).

GPU pools are pinned to zones where the accelerator actually exists (`node_locations`); a regional cluster otherwise spreads pools into every zone and creation fails in zones missing the GPU (L4 is absent from `us-central1-f`, for example).

```bash
gcloud container clusters get-credentials open-serve --region us-central1 --project <PROJECT_ID>
kubectl get nodes    # expect the system node(s) Ready
```

## 4. Software layer

```bash
NS=open-serve

kubectl create namespace $NS

# Worker ServiceAccount, bound to GCP via Workload Identity (model-bucket reads)
kubectl create serviceaccount open-serve-worker -n $NS
kubectl annotate serviceaccount open-serve-worker -n $NS \
  iam.gke.io/gcp-service-account=open-serve-worker@<PROJECT_ID>.iam.gserviceaccount.com

# API keys: {"sk-<source>-<hex>": "<source>"} — source labels usage metrics
API_KEY="sk-myteam-$(openssl rand -hex 16)"
kubectl create secret generic open-serve-api-keys -n $NS \
  --from-literal=key-map.json="{\"$API_KEY\": \"myteam\"}"

# KubeRay operator (upstream chart)
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator -n $NS

# open-serve
helm install open-serve charts/open-serve -n $NS -f my-values.yaml
```

A minimal first `my-values.yaml` (images default to the public GHCR releases — nothing to configure):

```yaml
monitoring:
  enabled: false        # enable after installing kube-prometheus-stack
probe:
  enabled: false
statusPage:
  enabled: false
externalGateway:
  enabled: false        # cluster-internal first; see the external HTTPS section

gateway:
  enabled: true
  defaultBackendUrl: "http://rayservice-qwen3-8b-serve-svc:8000"
  backends: []
  modelRoutes:
    Qwen/Qwen3-8B:
      service: rayservice-qwen3-8b-serve-svc
      port: 8000

serveModels: {}          # next step
```

## 5. Enable a model

Copy a preset from `catalog/models/` under `serveModels:` and set `enabled: true`. For the L4 pool, apply the preset's own L4 sizing row — e.g. Qwen3-8B:

```yaml
serveModels:
  qwen3-8b:
    enabled: true
    # ... body of catalog/models/qwen3-8b.yaml, with the L4 adjustments:
    vllmArgs:
      # (preset values, plus:)
      max_model_len: 8192
      max_num_seqs: 16
    gpu:
      count: 1
      acceleratorType: "L4"
      nodeSelector:
        workload-type: gpu-l4-1x
    resources:
      worker:
        requests: { cpu: "6", memory: "24Gi" }
        limits: { cpu: "7", memory: "26Gi" }
```

```bash
helm upgrade open-serve charts/open-serve -n $NS -f my-values.yaml --force-conflicts
```

!!! note "`--force-conflicts` on upgrades"
    Once a RayService is live, the KubeRay operator owns `workerGroupSpecs` (it writes autoscaled replica counts back). Helm v4's server-side apply refuses to override another field manager without this flag. Your values stay the source of truth.

First start on a fresh GPU node = node provisioning (~2 min) + runtime image pull (~10 GB) + model download from HF (~16 GB for an 8B model) + engine start: **15–20 minutes**. Watch:

```bash
kubectl -n $NS get rayservice,pods -w
```

Weights pull from the Hugging Face Hub by default; pre-stage to GCS (`model_source: gs://...`) to cut cold starts — the runtime mirrors any `gs://`/`s3://`/`hf://` source (see the [model catalog](../concepts/model-catalog.md)).

## 6. Validate

```bash
kubectl -n $NS port-forward svc/open-serve-gateway 8000:8000 &

curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/models          # 401 — auth enforced
curl -s -H "Authorization: Bearer $API_KEY" localhost:8000/v1/models       # your model listed

curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Hello!"}],"max_tokens":50}'
```

Or run the full suite: `BASE_URL=http://localhost:8000 API_KEY=$API_KEY scripts/test-endpoints.sh`.

## 7. Where next

- **Troubleshooting** — [`examples/gcp-quickstart/README.md`](https://github.com/hari-kathi/open-serve/blob/main/examples/gcp-quickstart/README.md) covers every failure we hit for real: registry 403s, zone/accelerator mismatches, free-tier `FailedScaleUp`, and the `GPUS_ALL_REGIONS` denial patterns.
- **External HTTPS** — enable `externalGateway` (GKE Gateway class + managed certs, or any Gateway API implementation + cert-manager).
- **Observability** — install kube-prometheus-stack, flip `monitoring.enabled`, and get the six Grafana dashboards, alert rules, probes, and the status page ([guide](../operations/observability.md)).
- **GitOps** — graduate from `helm install` to the FluxCD layout in [`deploy/flux/`](https://github.com/hari-kathi/open-serve/tree/main/deploy/flux).

## Teardown

```bash
cd examples/gcp-quickstart && tofu destroy
```

Delete any LoadBalancer Services/Gateways first if you created them (they hold cloud resources that block VPC deletion). Enabled APIs stay enabled by design.
