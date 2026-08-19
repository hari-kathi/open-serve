# GKE cluster for GPU model serving:
#   - one system pool for cluster services (gateways, controllers, operators)
#   - N GPU pools driven by the `gpu_pools` map, each tainted so only GPU
#     workloads land on them, scale-to-zero via total_min/total_max autoscaling
#   - Workload Identity + a worker service account for model-bucket access

data "google_project" "this" {
  project_id = var.project_id
}

# Nodes run as the default compute service account unless overridden. New GCP
# projects no longer auto-grant that SA any role, so without this binding
# kubelet gets 403 pulling from Artifact Registry in the same project
# (observed on a real fresh project). Disable if you manage node identity
# yourself.
resource "google_project_iam_member" "default_node_sa_registry_reader" {
  count   = var.grant_default_node_sa_registry_access ? 1 : 0
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"
}

locals {
  oauth_scopes = [
    "https://www.googleapis.com/auth/logging.write",
    "https://www.googleapis.com/auth/monitoring",
    "https://www.googleapis.com/auth/devstorage.read_only",
  ]
  image_type = "COS_CONTAINERD"

  # Zones passed to pools; null lets GKE use the cluster's default zones.
  node_locations = length(var.zones) > 0 ? var.zones : null

  # Cluster-autoscaler GPU limits per accelerator type, derived from the pool
  # map: upper bound if every pool using that accelerator scales to max. Set
  # without limits, the autoscaler can refuse GPU scale-ups
  # (no.scale.up.nap.pod.gpu.no.limit.defined) even with explicit pools.
  accelerator_types = distinct([for p in values(var.gpu_pools) : p.accelerator_type])
  gpu_resource_limits = {
    for t in local.accelerator_types :
    t => sum([for p in values(var.gpu_pools) : p.accelerator_count * p.max_nodes if p.accelerator_type == t])
  }
}

resource "google_container_cluster" "this" {
  name                = var.name
  project             = var.project_id
  location            = var.location
  network             = var.network
  subnetwork          = var.subnetwork
  deletion_protection = var.deletion_protection

  remove_default_node_pool = true
  initial_node_count       = 1

  workload_identity_config {
    workload_pool = "${data.google_project.this.project_id}.svc.id.goog"
  }

  private_cluster_config {
    enable_private_nodes    = var.enable_private_nodes
    enable_private_endpoint = var.enable_private_endpoint
    master_ipv4_cidr_block  = var.enable_private_nodes ? var.master_ipv4_cidr_block : null
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_secondary_range_name
    services_secondary_range_name = var.services_secondary_range_name
  }

  release_channel {
    channel = var.release_channel
  }

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = var.maintenance_start_time
    }
  }

  resource_labels = var.resource_labels

  cluster_autoscaling {
    autoscaling_profile = var.cluster_autoscaling_profile

    dynamic "resource_limits" {
      for_each = local.gpu_resource_limits
      content {
        resource_type = resource_limits.key
        minimum       = 0
        maximum       = resource_limits.value
      }
    }
  }
}

# --- System node pool ---
#
# Pools are named with a random_pet suffix tied to keeper values. Changing a
# keeper (machine type, generation bump) changes the name, which lets
# create_before_destroy stand up the replacement pool before the old one
# drains — no naming collision, no capacity gap.

resource "random_pet" "system" {
  keepers = {
    generation   = var.system_generation
    machine_type = var.system_machine_type
  }
}

resource "google_container_node_pool" "system" {
  name               = "system-${random_pet.system.id}"
  project            = var.project_id
  cluster            = google_container_cluster.this.id
  node_locations     = local.node_locations
  initial_node_count = 1

  autoscaling {
    total_min_node_count = var.system_min_nodes
    total_max_node_count = var.system_max_nodes
    location_policy      = "BALANCED"
  }

  node_config {
    machine_type = var.system_machine_type
    disk_size_gb = var.system_disk_size_gb
    disk_type    = "pd-ssd"

    oauth_scopes = local.oauth_scopes
    image_type   = local.image_type

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = {
      workload-type = "system"
    }
  }

  lifecycle {
    ignore_changes        = [initial_node_count]
    create_before_destroy = true
  }
}

# --- GPU node pools (data-driven) ---

resource "random_pet" "gpu" {
  for_each = var.gpu_pools

  keepers = {
    generation        = each.value.generation
    machine_type      = each.value.machine_type
    accelerator_type  = each.value.accelerator_type
    accelerator_count = each.value.accelerator_count
    spot              = each.value.spot
  }
}

resource "google_container_node_pool" "gpu" {
  for_each = var.gpu_pools

  name               = "${each.key}-${random_pet.gpu[each.key].id}"
  project            = var.project_id
  cluster            = google_container_cluster.this.id
  node_locations     = each.value.node_locations != null ? each.value.node_locations : local.node_locations
  initial_node_count = 0

  autoscaling {
    total_min_node_count = each.value.min_nodes
    total_max_node_count = each.value.max_nodes
    location_policy      = each.value.location_policy
  }

  node_config {
    machine_type = each.value.machine_type
    disk_size_gb = each.value.disk_size_gb
    disk_type    = each.value.disk_type
    spot         = each.value.spot

    guest_accelerator {
      type  = each.value.accelerator_type
      count = each.value.accelerator_count

      gpu_driver_installation_config {
        gpu_driver_version = each.value.gpu_driver_version
      }

      dynamic "gpu_sharing_config" {
        for_each = each.value.time_sharing_clients != null ? [1] : []
        content {
          gpu_sharing_strategy       = "TIME_SHARING"
          max_shared_clients_per_gpu = each.value.time_sharing_clients
        }
      }
    }

    oauth_scopes = local.oauth_scopes
    image_type   = local.image_type

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    labels = {
      workload-type = each.key
      gpu-type      = each.value.accelerator_type
      gpu-count     = tostring(each.value.accelerator_count)
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }
  }

  lifecycle {
    ignore_changes        = [initial_node_count]
    create_before_destroy = true
  }
}

# --- Worker identity (Workload Identity) ---

resource "google_service_account" "worker" {
  account_id   = var.worker_service_account_id
  display_name = "open-serve worker for ${var.name}"
  description  = "Used by serving pods (via Workload Identity) to read model weights and secrets"
  project      = var.project_id
}

resource "google_storage_bucket_iam_member" "worker_models" {
  count = var.model_bucket_name != null ? 1 : 0

  bucket = var.model_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_service_account_iam_member" "worker_workload_identity" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.workload_namespace}/${var.workload_ksa_name}]"

  depends_on = [google_container_cluster.this]
}

# Optional: Secret Manager secret for a Hugging Face token (gated model
# downloads). The secret is created empty — add a version out of band:
#   echo -n "$HF_TOKEN" | gcloud secrets versions add <secret-id> --data-file=-
resource "google_secret_manager_secret" "hf_token" {
  count = var.create_hf_token_secret ? 1 : 0

  secret_id = var.hf_token_secret_id
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "hf_token_accessor" {
  count = var.create_hf_token_secret ? 1 : 0

  secret_id = google_secret_manager_secret.hf_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}
