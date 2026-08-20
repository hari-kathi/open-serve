# AWS quickstart

Stands up a minimal open-serve environment in an existing AWS account:

1. **network** — VPC across 3 AZs: public subnets (NAT + load balancers) and private subnets (nodes), one NAT gateway
2. **cluster** — EKS cluster (private nodes, public endpoint, OIDC/IRSA, core addons) with:
   - `system` node group: m6i.large, 1–3 nodes — gateway, controllers, KubeRay operator, cluster autoscaler
   - `gpu-l4-1x` node group: g6.xlarge, 1x NVIDIA L4, 0–2 nodes, **spot** (an on-demand variant is included commented out)

The GPU node group scales to zero, so an idle cluster costs roughly the EKS control plane, the system node group, and the NAT gateway.

## Prerequisites

- An existing AWS account (this example does not create accounts or organizations)
- [OpenTofu](https://opentofu.org/) or Terraform >= 1.5
- `aws` CLI authenticated:

  ```sh
  aws configure --profile open-serve
  export AWS_PROFILE=open-serve
  ```

- Run the preflight script — it verifies credentials, checks the GPU vCPU
  quotas, and reports GPU instance availability in your region:

  ```sh
  ../../scripts/aws-preflight.sh us-east-1
  ```

- **GPU vCPU quota.** New AWS accounts have **zero** quota for both GPU
  instance classes, and the quotas are invisible until you look them up:
  - `Running On-Demand G and VT instances` (`L-DB2E81BA`) — for on-demand pools
  - `All G and VT Spot Instance Requests` (`L-3819A6DF`) — for spot pools (the default here)

  Request at least 4 vCPUs (one g6.xlarge) for whichever class you use —
  `../../scripts/aws-preflight.sh us-east-1 --request 8` files both. Increases
  are sometimes auto-approved in minutes, but **young accounts routinely get
  routed to manual review** (`CASE_OPENED` status) that takes days — request
  quota before you need it. Without it the node groups exist but every scale-up
  fails.

## Apply

```sh
cp terraform.tfvars.example terraform.tfvars   # edit region/profile
tofu init
tofu plan
tofu apply
```

EKS cluster creation takes ~10 minutes. Then configure kubectl (the exact
command is also in the outputs):

```sh
aws eks update-kubeconfig --region us-east-1 --name open-serve
```

## State

State is **local** (`terraform.tfstate` in this directory) to keep the
quickstart self-contained. For anything shared or long-lived, create an S3
bucket for state and uncomment the S3 backend stanza in `providers.tf`.

## Next: the software layer

Infrastructure alone serves nothing. Unlike GKE, EKS ships neither a GPU
device plugin nor a cluster autoscaler — install both, then KubeRay and the
open-serve chart:

```sh
# NVIDIA device plugin — advertises nvidia.com/gpu on the (tainted) GPU nodes.
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm install nvdp nvdp/nvidia-device-plugin -n kube-system \
  --set-json 'tolerations=[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"}]'

# Cluster autoscaler — scales the GPU node groups from zero. Use the
# cluster_autoscaler_role_arn output for the annotation.
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm install cluster-autoscaler autoscaler/cluster-autoscaler -n kube-system \
  --set autoDiscovery.clusterName=open-serve \
  --set awsRegion=us-east-1 \
  --set rbac.serviceAccount.name=cluster-autoscaler \
  --set image.tag=v1.31.2 \
  --set 'rbac.serviceAccount.annotations.eks\.amazonaws\.com/role-arn'=<autoscaler-role-arn>

# KubeRay operator + the open-serve chart — exactly like the GCP quickstart
# from here on (see docs/quickstart/aws.md for the full walkthrough).
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm install kuberay-operator kuberay/kuberay-operator -n open-serve --create-namespace
```

The `worker_role_arn` output is the IAM role to annotate on the
`open-serve-worker` Kubernetes service account
(`eks.amazonaws.com/role-arn: <arn>`) for IRSA access to model weights in S3.
See [`charts/open-serve/profiles/values-aws.yaml`](../../charts/open-serve/profiles/values-aws.yaml)
for a chart values profile matching this infrastructure.

## Teardown

```sh
tofu destroy
```

Notes:

- Anything created outside Terraform can block VPC deletion — Kubernetes-
  provisioned load balancers and their ENIs are the usual culprits. Delete
  `Service` objects of type LoadBalancer (and any Gateways/Ingresses) before
  `tofu destroy`, or the VPC will refuse to delete with `DependencyViolation`.
- Scale GPU node groups to zero (or just destroy) before walking away —
  spot GPU nodes left running still bill by the second.

## Troubleshooting

- **GPU pods stay `Pending`; the ASG scaling activity shows "You have
  requested more vCPU capacity than your current vCPU limit"** — the matching
  G/VT vCPU quota is still 0 (or too low). Check with
  `../../scripts/aws-preflight.sh`, request an increase, and note that spot
  and on-demand are **separate** quotas (`L-3819A6DF` vs `L-DB2E81BA`).
  Inspect activities with:
  `aws autoscaling describe-scaling-activities --region us-east-1 --max-items 10`
- **Spot scale-up fails with `InsufficientInstanceCapacity` or
  `SpotMaxPriceTooLow`** — spot stockout for that instance type. Managed node
  groups already use the capacity-optimized spot allocation strategy across
  the subnets you give them, so the usual fixes are more AZs (the default
  here is all three), another instance type (g6.2xlarge, g5.xlarge), or the
  on-demand variant pool.
- **GPU node joins but pods still `Pending` with `Insufficient
  nvidia.com/gpu`** — the NVIDIA device plugin is missing or does not
  tolerate the GPU taint, so the resource is never advertised. Install it
  with the toleration shown above and check
  `kubectl -n kube-system get ds nvdp-nvidia-device-plugin`.
- **`tofu destroy` hangs deleting the VPC** — leftover load-balancer ENIs.
  Delete the in-cluster LoadBalancer Services first, then re-run destroy.
- **cluster-autoscaler crash-loops with `AssumeRoleWithWebIdentity ... AccessDenied`**
  — the IRSA trust policy created by the cluster module names the ServiceAccount
  `kube-system/cluster-autoscaler`, but the upstream chart's *default* SA name is
  `<release>-aws-cluster-autoscaler`. Keep `--set rbac.serviceAccount.name=cluster-autoscaler`
  in the install command (already shown above) so the names match.
- **cluster-autoscaler runs but never scales anything; logs full of
  `ResourceSlice`/`DeviceClass` watch errors** — the chart's default image tag
  tracks the latest autoscaler, whose informers wait on resource APIs your
  cluster version doesn't serve, so the scaling loop never starts. Pin the
  image to your cluster's minor version (`--set image.tag=v1.31.2` for EKS
  1.31), per upstream's version-matching policy.
