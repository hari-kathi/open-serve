# End-to-end on AWS

Go from an AWS account to a GPU-backed, OpenAI-compatible serving endpoint on EKS. Every command below was executed against a real, freshly created account; the traps called out are ones we actually hit.

**What you'll build:** an EKS cluster with a CPU system node group and scale-to-zero GPU node groups; the NVIDIA device plugin and cluster autoscaler (the two things GKE gives you for free and EKS does not); KubeRay; the open-serve chart; and your first model from the catalog.

**Audience:** basic familiarity with Kubernetes, Terraform, and AWS. No Ray or vLLM knowledge needed.

!!! info "Validation status"
    Infrastructure, gateway auth/routing, and CPU-model serving are validated end-to-end on a fresh AWS account. GPU serving is validated up to node scheduling; the final hop depends only on your account's GPU vCPU quota (see step 2).

**Bring-your-own-cluster is first-class.** Already have EKS with GPU nodes and the device plugin? Skip to [step 4](#4-software-layer).

## 0. Tools

- `aws` CLI v2, `kubectl`, `helm` ≥ 3.9, [OpenTofu](https://opentofu.org/) or Terraform ≥ 1.5
- An IAM identity with admin (don't use root). A named profile keeps things tidy:

```bash
aws configure --profile open-serve     # access key, secret, region us-east-1
export AWS_PROFILE=open-serve
```

## 1. Preflight

```bash
scripts/aws-preflight.sh us-east-1
```

Verifies credentials, reads both GPU vCPU quotas (with any in-flight requests), and confirms your GPU instance type is offered in the region's AZs (`g6.xlarge` = 1× NVIDIA L4 is offered in five us-east-1 AZs).

## 2. GPU quota — start this now

AWS gates GPUs by **per-family vCPU quotas** that are 0 on new accounts and invisible until you look:

| Quota | Code | Gates |
|---|---|---|
| All G and VT Spot Instance Requests | `L-3819A6DF` | spot G-family (the default pool here) |
| Running On-Demand G and VT instances | `L-DB2E81BA` | on-demand G-family |

One g6.xlarge is 4 vCPUs; request 8 for headroom:

```bash
scripts/aws-preflight.sh us-east-1 --request 8
```

Day-old accounts get routed to human review (`CASE_OPENED`) that can take days — file this before anything else. Everything below works while it's pending.

## 3. Infrastructure

```bash
cd examples/aws-quickstart
cp terraform.tfvars.example terraform.tfvars   # region + profile
tofu init && tofu apply
```

~12–15 minutes. You get: a 3-AZ VPC (private node subnets, one NAT gateway), EKS with OIDC/IRSA, core addons, a `system` node group (m6i.large, 1–3, autoscaler-managed), a spot `gpu-l4-1x` node group (g6.xlarge, 0–2, `AL2023_x86_64_NVIDIA` AMI — NVIDIA drivers baked in) carrying the scheduling contract (`workload-type` label + `nvidia.com/gpu` taint), and two IRSA roles: one for worker pods (S3 model reads) and one for the cluster autoscaler.

```bash
aws eks update-kubeconfig --region us-east-1 --name open-serve
kubectl get nodes    # system node Ready
```

Note the two Terraform outputs you'll use below: `worker_role_arn` and `cluster_autoscaler_role_arn`.

## 4. Software layer

EKS ships neither a GPU device plugin nor a cluster autoscaler. Install both first:

```bash
# NVIDIA device plugin — advertises nvidia.com/gpu on the tainted GPU nodes
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm install nvdp nvdp/nvidia-device-plugin -n kube-system \
  --set-json 'tolerations=[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"}]'

# Cluster autoscaler — scales node groups (incl. GPU from zero)
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install cluster-autoscaler autoscaler/cluster-autoscaler -n kube-system \
  --set autoDiscovery.clusterName=open-serve \
  --set awsRegion=us-east-1 \
  --set rbac.serviceAccount.name=cluster-autoscaler \
  --set image.tag=v1.31.2 \
  --set 'rbac.serviceAccount.annotations.eks\.amazonaws\.com/role-arn'=<cluster_autoscaler_role_arn>
```

!!! warning "Two autoscaler settings that are not optional"
    Both were found the hard way on a real deployment:

    - `rbac.serviceAccount.name=cluster-autoscaler` — the IRSA trust policy names this SA; the chart's *default* name (`<release>-aws-cluster-autoscaler`) mismatches it and produces an `AssumeRoleWithWebIdentity AccessDenied` crash loop.
    - `image.tag=v1.31.2` — **match the autoscaler minor to your cluster minor** (here 1.31). The chart default tracks latest, whose informers wait on resource APIs your cluster doesn't have, and the scaling loop never runs — no errors you'd notice, just no scaling.

Then the serving stack:

```bash
NS=open-serve
kubectl create namespace $NS

# Worker ServiceAccount, bound to AWS via IRSA (S3 model reads)
kubectl create serviceaccount open-serve-worker -n $NS
kubectl annotate serviceaccount open-serve-worker -n $NS \
  eks.amazonaws.com/role-arn=<worker_role_arn>

API_KEY="sk-myteam-$(openssl rand -hex 16)"
kubectl create secret generic open-serve-api-keys -n $NS \
  --from-literal=key-map.json="{\"$API_KEY\": \"myteam\"}"

helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator -n $NS

helm install open-serve charts/open-serve -n $NS -f my-values.yaml
```

Start `my-values.yaml` from [`charts/open-serve/profiles/values-aws.yaml`](https://github.com/hari-kathi/open-serve/blob/main/charts/open-serve/profiles/values-aws.yaml) — same shape as the GCP guide's minimal values (images default to the public GHCR releases; nothing to configure there).

## 5. Enable a model

Identical to [the GCP guide, step 5](gcp.md#5-enable-a-model), with g6.xlarge sizing — it has 4 vCPUs / 16 GiB, so the worker resources are tighter than an A100 host:

```yaml
serveModels:
  qwen3-8b:
    enabled: true
    # ... body of catalog/models/qwen3-8b.yaml, with:
    sharedMemorySize: "4Gi"
    vllmArgs:
      max_model_len: 8192
      max_num_seqs: 16
    gpu:
      count: 1
      acceleratorType: "L4"
      nodeSelector:
        workload-type: gpu-l4-1x
    resources:
      worker:
        requests: { cpu: "3", memory: "10Gi" }
        limits: { cpu: "3500m", memory: "12Gi" }
```

(`g6.2xlarge` — 8 vCPU / 32 GiB — is the roomier single-L4 option.) Upgrades of live models need `--force-conflicts`; see [the note in the GCP guide](gcp.md#5-enable-a-model).

## 6. Validate

Same as [GCP step 6](gcp.md#6-validate): port-forward `svc/open-serve-gateway`, expect 401 without the key, your model in `/v1/models`, and a chat completion with it.

## 7. Where next

- **Troubleshooting** — [`examples/aws-quickstart/README.md`](https://github.com/hari-kathi/open-serve/blob/main/examples/aws-quickstart/README.md): quota-zero ASG messages, spot `InsufficientInstanceCapacity`, missing device plugin, both autoscaler traps, VPC teardown blockers.
- **External access** — AWS Load Balancer Controller + Gateway API/Ingress, or keep it VPC-internal.
- **Observability / GitOps** — same as GCP: kube-prometheus-stack + `monitoring.enabled`, and the FluxCD layout in `deploy/flux/`.

## Teardown

```bash
cd examples/aws-quickstart && tofu destroy
```

Delete LoadBalancer Services first if you created any (their ENIs block VPC deletion), and don't leave spot GPU nodes running when you walk away.
