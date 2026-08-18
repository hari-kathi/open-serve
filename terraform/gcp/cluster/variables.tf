# --- General ---

variable "name" {
  type        = string
  description = "Name of the GKE cluster."
  default     = "open-serve"
}

variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "location" {
  type        = string
  description = "Region for the (regional) cluster."
  default     = "us-central1"
}

variable "zones" {
  type        = list(string)
  description = "Zones for node pool placement. Empty means all zones in the region."
  default     = []
}

# --- Networking ---

variable "network" {
  type        = string
  description = "VPC network self link or ID."
}

variable "subnetwork" {
  type        = string
  description = "Subnet self link or ID."
}

variable "pods_secondary_range_name" {
  type        = string
  description = "Name of the subnet secondary range for pods."
}

variable "services_secondary_range_name" {
  type        = string
  description = "Name of the subnet secondary range for services."
}

variable "enable_private_nodes" {
  type        = bool
  description = "Give nodes internal IPs only (egress via Cloud NAT)."
  default     = true
}

variable "enable_private_endpoint" {
  type        = bool
  description = "Make the control-plane endpoint private-only. Leave false for a public endpoint (kubectl from anywhere)."
  default     = false
}

variable "master_ipv4_cidr_block" {
  type        = string
  description = "RFC1918 /28 block for the GKE control plane (required with private nodes)."
  default     = "172.16.0.0/28"
}

# --- Cluster settings ---

variable "release_channel" {
  type        = string
  description = "GKE release channel."
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE", "UNSPECIFIED"], var.release_channel)
    error_message = "release_channel must be RAPID, REGULAR, STABLE, or UNSPECIFIED."
  }
}

variable "deletion_protection" {
  type        = bool
  description = "Refuse to delete the cluster via Terraform. Enable for long-lived environments."
  default     = false
}

variable "resource_labels" {
  type        = map(string)
  description = "GCP resource labels applied to the cluster."
  default = {
    project = "open-serve"
  }
}

variable "maintenance_start_time" {
  type        = string
  description = "Daily maintenance window start time (HH:MM, UTC)."
  default     = "07:00"
}

variable "cluster_autoscaling_profile" {
  type        = string
  description = "Cluster autoscaler profile."
  default     = "BALANCED"

  validation {
    condition     = contains(["BALANCED", "OPTIMIZE_UTILIZATION"], var.cluster_autoscaling_profile)
    error_message = "cluster_autoscaling_profile must be BALANCED or OPTIMIZE_UTILIZATION."
  }
}

# --- System node pool ---

variable "system_machine_type" {
  type        = string
  description = "Machine type for the system node pool."
  default     = "e2-standard-4"
}

variable "system_min_nodes" {
  type        = number
  description = "Minimum total nodes in the system pool (across all zones)."
  default     = 1
}

variable "system_max_nodes" {
  type        = number
  description = "Maximum total nodes in the system pool (across all zones)."
  default     = 3
}

variable "system_disk_size_gb" {
  type        = number
  description = "Disk size in GB for system nodes."
  default     = 100
}

variable "system_generation" {
  type        = number
  description = "Bump to force recreation of the system node pool."
  default     = 1
}

# --- GPU node pools ---

# Map of pool name => pool spec. The key becomes the pool's name prefix and its
# workload-type node label; schedule onto a pool with
#   nodeSelector: { workload-type: "<key>" }
variable "gpu_pools" {
  type = map(object({
    machine_type         = string
    accelerator_type     = string
    accelerator_count    = number
    min_nodes            = number
    max_nodes            = number
    disk_size_gb         = optional(number, 200)
    disk_type            = optional(string, "pd-ssd")
    spot                 = optional(bool, false)
    node_locations       = optional(list(string))
    gpu_driver_version   = optional(string, "LATEST")
    time_sharing_clients = optional(number)
    location_policy      = optional(string, "ANY")
    generation           = optional(number, 1)
  }))
  description = "GPU node pools, keyed by pool name. min_nodes/max_nodes are totals across zones; location_policy ANY targets whichever zone has GPU stock. Bump generation to force pool recreation."
  default     = {}

  validation {
    condition     = alltrue([for p in var.gpu_pools : contains(["ANY", "BALANCED"], p.location_policy)])
    error_message = "location_policy must be ANY or BALANCED."
  }
}

# --- Worker identity (Workload Identity) ---

variable "worker_service_account_id" {
  type        = string
  description = "Account ID for the GCP service account used by serving workloads."
  default     = "open-serve-worker"
}

variable "workload_namespace" {
  type        = string
  description = "Kubernetes namespace of the serving workloads."
  default     = "open-serve"
}

variable "workload_ksa_name" {
  type        = string
  description = "Kubernetes service account name bound to the worker GCP SA via Workload Identity."
  default     = "open-serve-worker"
}

variable "model_bucket_name" {
  type        = string
  description = "GCS bucket with model weights. When set, the worker SA gets roles/storage.objectViewer on it."
  default     = null
}

variable "create_hf_token_secret" {
  type        = bool
  description = "Create an empty Secret Manager secret for a Hugging Face token and grant the worker SA accessor. Add the token value out of band."
  default     = false
}

variable "hf_token_secret_id" {
  type        = string
  description = "Secret Manager secret ID for the Hugging Face token."
  default     = "open-serve-hf-token"
}
