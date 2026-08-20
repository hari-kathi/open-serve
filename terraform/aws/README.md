# AWS reference modules

Two composable modules for a turnkey open-serve environment on AWS. See
[`examples/aws-quickstart/`](../../examples/aws-quickstart/) for a wired-up root module.

```
network/     # VPC, public + private subnets across 3 AZs, IGW, NAT
cluster/     # EKS: system node group + data-driven GPU node groups, OIDC/IRSA
```

There is no bootstrap module — AWS has no API-enablement step. Run
[`scripts/aws-preflight.sh`](../../scripts/aws-preflight.sh) instead; it verifies
credentials and the GPU vCPU quotas (which default to **0** on new accounts).

## `network/`

| Input | Default | Purpose |
|---|---|---|
| `name` | `open-serve` | Base name for VPC/subnets/NAT |
| `cluster_name` | `open-serve` | Value for the `kubernetes.io/cluster/<name>=shared` subnet tags |
| `vpc_cidr` | `10.20.0.0/16` | VPC CIDR, carved into /20 subnets per AZ |
| `az_count` | `3` | AZs to spread subnets across |
| `nat_per_az` | `false` | One NAT gateway per AZ (HA) instead of a single shared one |
| `tags` | `{}` | Extra tags on everything |

Public subnets (NAT + external LBs) are tagged `kubernetes.io/role/elb=1`,
private subnets (nodes + internal LBs) `kubernetes.io/role/internal-elb=1`.
The single-NAT default is the cost-conscious choice; flip `nat_per_az` for HA.

Outputs: `vpc_id`, `private_subnet_ids`, `public_subnet_ids`,
`availability_zones`, `nat_public_ips`.

## `cluster/`

EKS cluster (default version `1.31`) on the private subnets with a public +
private control-plane endpoint (both toggleable), an IAM OIDC provider for
IRSA, the core addons (`vpc-cni`, `coredns`, `kube-proxy`,
`eks-pod-identity-agent`), and:

- **One system node group** — `system_instance_type` (default m6i.large),
  `system_min_nodes`/`system_max_nodes` (1/3), labeled `workload-type=system`,
  `AL2023_x86_64_STANDARD`.
- **GPU node groups from a map** — `gpu_pools = { <name> = { ... } }`; each entry:

  ```hcl
  {
    instance_type = "g6.xlarge"
    gpu_type      = "nvidia-l4"  # informational label
    gpu_count     = 1            # optional
    min_nodes     = 0            # optional; 0 = scale-to-zero
    max_nodes     = 2
    spot          = false        # optional; SPOT capacity type
    disk_size_gb  = 200          # optional
    subnet_ids    = null         # optional subnet/AZ override
  }
  ```

  Every GPU node group uses `AL2023_x86_64_NVIDIA` (NVIDIA drivers baked into
  the AMI), gets the `nvidia.com/gpu=present:NO_SCHEDULE` taint, labels
  `workload-type=<name>`, `gpu-type`, `gpu-count`, and the cluster-autoscaler
  discovery tags (`k8s.io/cluster-autoscaler/enabled=true`,
  `k8s.io/cluster-autoscaler/<cluster>=owned`). Scale-from-zero is handled by
  the cluster autoscaler, which is installed in the software layer (EKS has no
  built-in equivalent of GKE's node auto-provisioning).
- **Node IAM role** — shared by all node groups, with
  `AmazonEKSWorkerNodePolicy`, `AmazonEC2ContainerRegistryReadOnly`, and
  `AmazonEKS_CNI_Policy`.
- **Worker identity (IRSA)** — an IAM role assumable only by the
  `<workload_namespace>/<workload_ksa_name>` service account (defaults
  `open-serve`/`open-serve-worker`), granted `s3:GetObject`/`s3:ListBucket`
  on `model_bucket_name` when set. Annotate the KSA with
  `eks.amazonaws.com/role-arn: <worker_role_arn output>`.
- **Cluster-autoscaler identity (IRSA)** — an IAM role for
  `kube-system/cluster-autoscaler` with the standard autoscaler policy;
  mutating actions are scoped to ASGs tagged
  `k8s.io/cluster-autoscaler/<cluster>=owned`.
- **Optional S3 model bucket** — `create_model_bucket = true` +
  `model_bucket_name` (versioning off, public access blocked).

Outputs: cluster name/endpoint/CA, OIDC provider ARN, node role ARN, both
IRSA role ARNs, GPU node group names, `update_kubeconfig_command`.

## Validation

Each directory is a standalone module:

```sh
tofu init -backend=false && tofu validate && tofu fmt -check -recursive
```
