# GCP reference modules

Three composable modules for a turnkey open-serve environment on GCP. See
[`examples/gcp-quickstart/`](../../examples/gcp-quickstart/) for a wired-up root module.

```
bootstrap/   # enable APIs on an existing project, optional state + model buckets
network/     # VPC, GKE-ready subnet, Cloud Router + NAT
cluster/     # regional GKE: system pool + data-driven GPU pools, Workload Identity
```

## `bootstrap/`

Prepares an **existing** project (no project creation — bring your own).

| Input | Default | Purpose |
|---|---|---|
| `project_id` | — | Project to bootstrap |
| `apis` | container, compute, monitoring, logging, cloudresourcemanager, servicenetworking, secretmanager, artifactregistry, iam | APIs to enable (`disable_on_destroy = false`) |
| `create_state_bucket` / `state_bucket_name` | `false` / `null` | Versioned GCS bucket for Terraform state |
| `create_model_bucket` / `model_bucket_name` | `false` / `null` | Model-weights bucket (versioning off, uniform bucket-level access, public access prevention enforced) |
| `bucket_location` | `US` | Bucket location |

Outputs: `enabled_services`, `state_bucket_name`, `model_bucket_name`.

## `network/`

| Input | Default | Purpose |
|---|---|---|
| `name` | `open-serve` | Base name for VPC/subnet/router/NAT |
| `project_id`, `region` | — / `us-central1` | Placement |
| `subnet_cidr_range` | `10.10.0.0/20` | Node subnet |
| `pods_cidr_range` | `10.12.0.0/16` | Pods secondary range |
| `services_cidr_range` | `10.13.0.0/20` | Services secondary range |
| `enable_flow_logs` | `false` | VPC flow logs |
| `nat_ip_count` | `1` | Static NAT IPs; `0` = auto-allocate |

Outputs: network/subnet names, IDs and self links, secondary range names, NAT IPs.
Private Google access is always on.

## `cluster/`

Regional GKE cluster with private nodes + public endpoint (both toggleable),
Workload Identity, Gateway API (`CHANNEL_STANDARD`), and:

- **One system pool** — `system_machine_type` (default e2-standard-4),
  `system_min_nodes`/`system_max_nodes` (1/3), labeled `workload-type=system`.
- **GPU pools from a map** — `gpu_pools = { <name> = { ... } }`; each entry:

  ```hcl
  {
    machine_type         = "g2-standard-8"
    accelerator_type     = "nvidia-l4"
    accelerator_count    = 1
    min_nodes            = 0        # totals across zones; 0 = scale-to-zero
    max_nodes            = 2
    disk_size_gb         = 200      # optional
    disk_type            = "pd-ssd" # optional
    spot                 = false    # optional
    node_locations       = null     # optional zone override
    gpu_driver_version   = "LATEST" # optional
    time_sharing_clients = null     # optional GPU time-sharing
    location_policy      = "ANY"    # optional; ANY chases GPU stock across zones
    generation           = 1        # optional; bump to force pool replacement
  }
  ```

  Every GPU pool gets the `nvidia.com/gpu=present:NO_SCHEDULE` taint, labels
  `workload-type=<name>`, `gpu-type`, `gpu-count`, and scale-to-zero autoscaling.
  Cluster-autoscaler GPU `resource_limits` are computed from the map per
  accelerator type. Pool names carry a `random_pet` suffix keyed on machine
  type/accelerator/generation so replacements are create-before-destroy.

- **Worker identity** — creates a GCP SA (`worker_service_account_id`, default
  `open-serve-worker`), binds it via Workload Identity to
  `<workload_namespace>/<workload_ksa_name>` (defaults `open-serve`/`open-serve-worker`),
  grants `roles/storage.objectViewer` on `model_bucket_name` when set, and can
  create a Secret Manager secret for a Hugging Face token
  (`create_hf_token_secret`).

Outputs: cluster name/ID/endpoint/CA, worker SA email, system + GPU pool names.

## Validation

Each directory is a standalone module:

```sh
tofu init -backend=false && tofu validate && tofu fmt -check -recursive
```
