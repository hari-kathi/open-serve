# GCP quickstart

Stands up a minimal open-serve environment in an existing GCP project:

1. **bootstrap** — enables the required Google APIs
2. **network** — VPC, GKE-ready subnet (pods/services secondary ranges), Cloud Router + NAT
3. **cluster** — regional GKE cluster (private nodes, public endpoint, Workload Identity, Gateway API) with:
   - `system` pool: e2-standard-4, 1–3 nodes — gateways, controllers, KubeRay operator
   - `gpu-l4-1x` pool: g2-standard-8, 1x NVIDIA L4, 0–2 nodes, **spot**
   - `gpu-a100-1x` pool: a2-highgpu-1g, 1x NVIDIA A100 40GB, 0–1 nodes

Both GPU pools scale to zero, so an idle cluster costs roughly the system pool plus network baseline.

## Prerequisites

- An existing GCP project with billing linked (this example does not create projects)
- [OpenTofu](https://opentofu.org/) or Terraform >= 1.5
- Run the preflight script — it verifies auth/project/billing, **enables all
  required APIs**, and reports GPU quota gaps with request instructions:

  ```sh
  ../../scripts/gcp-preflight.sh <PROJECT_ID> [REGION]
  ```

- `gcloud` authenticated with application default credentials:

  ```sh
  gcloud auth login
  gcloud auth application-default login
  ```

  (or, for a short-lived non-interactive alternative:
  `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)` — valid
  ~1 hour; re-export and re-run apply if it expires mid-apply)

- **GPU quota.** New projects usually have zero GPU quota. Request it in
  IAM & Admin → Quotas before applying, or the pools will fail to scale up:
  - `NVIDIA L4 GPUs` (and `Preemptible NVIDIA L4 GPUs` for the spot pool) in your region
  - `NVIDIA A100 GPUs` in your region

  Quota requests for 1–2 GPUs are typically approved quickly; A100 quota can take longer.

## Apply

```sh
cp terraform.tfvars.example terraform.tfvars   # edit project_id
tofu init
tofu plan
tofu apply
```

The API-enablement step can take a few minutes on a fresh project. If the cluster
module races an API that is still propagating, just re-run `tofu apply`.

Then configure kubectl (the exact command is also in the outputs):

```sh
gcloud container clusters get-credentials open-serve --region us-central1 --project <project-id>
```

## State

State is **local** (`terraform.tfstate` in this directory) to keep the quickstart
self-contained. For anything shared or long-lived, create a state bucket
(`create_state_bucket = true` on the bootstrap module) and uncomment the GCS
backend stanza in `providers.tf`.

## Next: the software layer

Infrastructure alone serves nothing. Head to [`deploy/flux/`](../../deploy/flux/)
to install the open-serve stack (KubeRay, runtimes, gateway) onto the cluster.
The `worker_service_account_email` output is the GCP SA to annotate on the
`open-serve-worker` Kubernetes service account for Workload Identity.

## Teardown

```sh
tofu destroy
```

Notes:

- The cluster is created with `deletion_protection = false`, so destroy works
  directly. If you enabled it, flip it off and apply before destroying.
- Anything created outside Terraform (Kubernetes-provisioned load balancers,
  gateways, persistent disks) can block VPC deletion — clean up in-cluster
  resources (or delete the Gateway/Service objects) before `tofu destroy`.
- Enabled APIs are left enabled on destroy by design.

## Troubleshooting

- **Pods stuck in `ImagePullBackOff` with `403 Forbidden` from Artifact
  Registry** — new GCP projects grant the default compute service account no
  roles, so kubelet cannot pull images even from the same project. The cluster
  module now grants `roles/artifactregistry.reader` automatically
  (`grant_default_node_sa_registry_access = true`); if you disabled it, grant
  your node service account registry read access yourself.
- **GPU nodes never appear; autoscaler logs `FailedScaleUp ... Internal error`**
  — check `gcloud compute operations list --filter='httpStatus>=400'`. If the
  message is *"billing account is currently in the free tier where non-TPU
  accelerators are not available"*, the billing account must be upgraded to a
  paid account (Console → Billing → Upgrade). GPU quota alone is not
  sufficient; free-tier billing blocks all GPU instance creation. The pending
  pod schedules automatically once the autoscaler retries after the upgrade.
- **GPU pool creation fails with `Accelerator type ... does not exist in zone`**
  — regional clusters place pools in every zone unless `node_locations`
  restricts them, and not every zone carries every GPU. List availability with
  `gcloud compute accelerator-types list --filter="zone:<region>"` and set
  `node_locations` on the pool.
