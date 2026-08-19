# Minimal open-serve environment: APIs -> VPC/NAT -> GKE with a system pool
# and two scale-to-zero GPU pools (spot L4 for cheap experimentation, A100 for
# bigger models).

module "bootstrap" {
  source = "../../terraform/gcp/bootstrap"

  project_id = var.project_id
}

module "network" {
  source = "../../terraform/gcp/network"

  name       = "open-serve"
  project_id = var.project_id
  region     = var.region

  depends_on = [module.bootstrap]
}

module "cluster" {
  source = "../../terraform/gcp/cluster"

  name       = "open-serve"
  project_id = var.project_id
  location   = var.region

  network                       = module.network.network_self_link
  subnetwork                    = module.network.subnet_self_link
  pods_secondary_range_name     = module.network.pods_secondary_range_name
  services_secondary_range_name = module.network.services_secondary_range_name

  # System pool: cluster services (gateway, controllers, KubeRay operator).
  system_machine_type = "e2-standard-4"
  system_min_nodes    = 1
  system_max_nodes    = 3

  gpu_pools = {
    # Spot L4: cheap single-GPU serving; pods must tolerate preemption.
    # node_locations restricts the pool to zones where the accelerator exists —
    # a regional cluster otherwise spreads pools over ALL zones and pool
    # creation fails in any zone missing the GPU (L4 is absent from
    # us-central1-f, for example). Check availability for your region with:
    #   gcloud compute accelerator-types list --filter="zone:<region>"
    gpu-l4-1x = {
      machine_type      = "g2-standard-8"
      accelerator_type  = "nvidia-l4"
      accelerator_count = 1
      min_nodes         = 0
      max_nodes         = 2
      spot              = true
      node_locations    = ["us-central1-a", "us-central1-b", "us-central1-c"]
    }

    # On-demand A100 40GB for larger models.
    gpu-a100-1x = {
      machine_type      = "a2-highgpu-1g"
      accelerator_type  = "nvidia-tesla-a100"
      accelerator_count = 1
      min_nodes         = 0
      max_nodes         = 1
    }
  }

  depends_on = [module.bootstrap]
}
