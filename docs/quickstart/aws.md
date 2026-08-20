# End-to-end on AWS

Go from a fresh AWS account to a GPU-backed, OpenAI-compatible serving endpoint. The path has two layers:

1. **Infrastructure** — reference Terraform for EKS + GPU node groups (`examples/aws-quickstart/`, backed by the modules in `terraform/aws/`).
2. **Software** — Helm installs for the pieces EKS does not ship (NVIDIA device plugin, cluster autoscaler), then KubeRay and the open-serve chart.

!!! warning "Validation status"
    The open-serve stack itself is validated on kind locally (see the [kind quickstart](kind.md)). The AWS end-to-end path — reference Terraform plus the Helm software layer on EKS — is **under active validation** ahead of v0.1.0; expect rough edges and check `examples/aws-quickstart/` for the current state.

**Bring-your-own-cluster is a first-class path.** If you already have an EKS (or any Kubernetes) cluster with GPU nodes, skip straight to [the software layer](#2-software-layer-helm).

## Prerequisites

- An AWS account, and the `aws` CLI authenticated (`aws configure --profile open-serve && export AWS_PROFILE=open-serve`)
- OpenTofu or Terraform >= 1.5
- `kubectl` and `helm`

## 0. Preflight

Run the preflight script before anything else. It verifies credentials, checks the two GPU vCPU quotas, and reports GPU instance-type availability in your region's AZs:

```bash
scripts/aws-preflight.sh [REGION]                # region defaults to us-east-1
scripts/aws-preflight.sh us-east-1 --request 8   # also file quota increase requests
```

It is safe to re-run at any time and exits non-zero while action items remain.

!!! warning "GPU vCPU quota"
    New AWS accounts have **zero** quota for both GPU instance classes — and the quotas are invisible until you look them up:

    - `Running On-Demand G and VT instances` (quota code `L-DB2E81BA`)
    - `All G and VT Spot Instance Requests` (quota code `L-3819A6DF`)

    Spot and on-demand are separate quotas; the quickstart's default pool is spot. Request at least 4 vCPUs (one g6.xlarge) *before* applying. Increases are sometimes auto-approved in minutes, but young accounts routinely get routed to manual review (`CASE_OPENED`) that takes days. Without quota, GPU node groups exist but every scale-up fails and pods stay `Pending`.

## 1. Infrastructure layer (Terraform)

The reference root in `examples/aws-quickstart/` wires two modules (from `terraform/aws/`) into one apply:

| Module | What it creates |
|---|---|
| `network` | VPC across 3 AZs — public subnets (NAT + LBs) and private subnets (nodes), one NAT gateway |
| `cluster` | The EKS cluster, system + GPU managed node groups (scale-to-zero, spot, data-driven pool map), OIDC provider, and the IRSA roles for the serving workers and the cluster autoscaler |

```bash
cd examples/aws-quickstart
cp terraform.tfvars.example terraform.tfvars   # edit region/profile
tofu init && tofu apply                        # or terraform
```

See `terraform/aws/README.md` for module inputs (instance types, pool sizing, spot). Once the cluster exists (~10 minutes):

```bash
aws eks update-kubeconfig --region us-east-1 --name open-serve
kubectl get nodes
```

Note two Terraform outputs for the next section: `worker_role_arn` and `cluster_autoscaler_role_arn`.

## 2. Software layer (Helm)

Unlike GKE, EKS ships neither a GPU device plugin nor a cluster autoscaler — the GPU AMI has NVIDIA drivers baked in, but nothing advertises `nvidia.com/gpu`, and nothing scales node groups from zero. Install both, then KubeRay and the chart.

### NVIDIA device plugin

The toleration matters: the GPU nodes carry the `nvidia.com/gpu=present:NO_SCHEDULE` taint, and without it the plugin never lands on them.

```bash
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm install nvdp nvdp/nvidia-device-plugin -n kube-system \
  --set-json 'tolerations=[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"}]'
```

### Cluster autoscaler

Discovers the GPU node groups via their `k8s.io/cluster-autoscaler/*` tags (set by the Terraform) and scales them from zero. The service-account annotation wires it to its IAM role via IRSA — use the `cluster_autoscaler_role_arn` output:

```bash
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install cluster-autoscaler autoscaler/cluster-autoscaler -n kube-system \
  --set autoDiscovery.clusterName=open-serve \
  --set awsRegion=us-east-1 \
  --set 'rbac.serviceAccount.annotations.eks\.amazonaws\.com/role-arn'=<cluster_autoscaler_role_arn>
```

### KubeRay operator

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator \
  -n open-serve --create-namespace --version 1.6.2
```

### Worker ServiceAccount (IRSA)

The chart requires the worker ServiceAccount to pre-exist. Create it and annotate it with the `worker_role_arn` output so serving pods can read model weights from S3:

```bash
kubectl -n open-serve create serviceaccount open-serve-worker
kubectl -n open-serve annotate serviceaccount open-serve-worker \
  eks.amazonaws.com/role-arn=<worker_role_arn>
```

### API-key Secret

The gateway authenticates against a key map in the `open-serve-api-keys` Secret, which is deliberately not in Git. Create it once per cluster (full format, rotation, and conventions: [API keys](../operations/api-keys.md)):

```bash
kubectl -n open-serve create secret generic open-serve-api-keys \
  --from-literal=key-map.json='{"sk-team1-<hex>": "team1"}'
```

### Install the chart

Start from the AWS profile in `charts/open-serve/profiles/values-aws.yaml` — it matches the quickstart's node groups and includes a commented `qwen3-8b` example sized for the g6.xlarge L4 pool (reduced `max_model_len`/`max_num_seqs`, `nodeSelector: workload-type: gpu-l4-1x`). Copy it, uncomment/adjust the model, then:

```bash
helm install open-serve oci://ghcr.io/hari-kathi/charts/open-serve \
  -n open-serve -f my-values.yaml
```

KubeRay creates `rayservice-qwen3-8b`, the cluster autoscaler provisions a GPU node (watch for quota errors in the ASG scaling activities if it never appears), and the first spin-up includes image pull + model load — tens of minutes is normal for large images.

### Validate

```bash
kubectl -n open-serve get rayservice,pods
kubectl -n open-serve port-forward svc/open-serve-gateway 8000:8000
```

```bash
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer sk-team1-<hex>"

curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-team1-<hex>" -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen3-8B", "messages": [{"role": "user", "content": "Say hi."}]}'
```

Or run the scripted checks: `BASE_URL=http://localhost:8000 API_KEY=sk-team1-<hex> CHAT_MODELS="qwen3-8b" ./scripts/test-endpoints.sh`.

## Troubleshooting

- **GPU pods `Pending`, ASG activity says "You have requested more vCPU capacity than your current vCPU limit"** — the matching G/VT vCPU quota is still 0. See [Preflight](#0-preflight); remember spot and on-demand are separate quotas.
- **Spot scale-up fails (`InsufficientInstanceCapacity`, `SpotMaxPriceTooLow`)** — spot stockout. Managed node groups already use capacity-optimized allocation across your subnets; add AZs, switch instance type, or use the on-demand variant pool in `examples/aws-quickstart/main.tf`.
- **GPU node joined but `nvidia.com/gpu` never advertised** — device plugin missing or not tolerating the GPU taint (see above).
- **Teardown blocked (`DependencyViolation` on the VPC)** — Kubernetes-provisioned load balancers and their ENIs; delete LoadBalancer Services before `tofu destroy`.

## Next steps

- [Adding a model](../operations/adding-a-model.md) — the full runbook
- [Observability](../operations/observability.md) — dashboards, alerts, probe, status page (needs `monitoring.enabled: true` + kube-prometheus-stack)
- [Cost](../operations/cost.md) — scale-to-zero, autoscaling caps, spot pools
