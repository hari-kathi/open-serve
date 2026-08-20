# --- General ---

variable "name" {
  type        = string
  description = "Name of the EKS cluster. Also prefixes the IAM roles this module creates."
  default     = "open-serve"
}

variable "kubernetes_version" {
  type        = string
  description = "EKS Kubernetes version."
  default     = "1.31"
}

# --- Networking ---

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for the cluster and node groups (from the network module)."
}

variable "endpoint_public_access" {
  type        = bool
  description = "Expose the control-plane endpoint publicly (kubectl from anywhere)."
  default     = true
}

variable "endpoint_private_access" {
  type        = bool
  description = "Enable in-VPC access to the control-plane endpoint (nodes reach the API without leaving the VPC)."
  default     = true
}

variable "public_access_cidrs" {
  type        = list(string)
  description = "CIDR blocks allowed to reach the public control-plane endpoint."
  default     = ["0.0.0.0/0"]
}

# --- Cluster settings ---

variable "enabled_log_types" {
  type        = list(string)
  description = "Control-plane log types shipped to CloudWatch (api, audit, authenticator, controllerManager, scheduler). Empty disables control-plane logging (no CloudWatch cost)."
  default     = []
}

variable "tags" {
  type        = map(string)
  description = "Extra tags applied to every resource in this module."
  default = {
    project = "open-serve"
  }
}

# --- System node group ---

variable "system_instance_type" {
  type        = string
  description = "Instance type for the system node group."
  default     = "m6i.large"
}

variable "system_min_nodes" {
  type        = number
  description = "Minimum nodes in the system node group."
  default     = 1
}

variable "system_max_nodes" {
  type        = number
  description = "Maximum nodes in the system node group."
  default     = 3
}

variable "system_desired_nodes" {
  type        = number
  description = "Initial desired nodes in the system node group (drift after creation is ignored; the cluster autoscaler owns it)."
  default     = 1
}

variable "system_disk_size_gb" {
  type        = number
  description = "Disk size in GB for system nodes."
  default     = 100
}

# --- GPU node groups ---

# Map of pool name => pool spec. The key becomes the node group name and its
# workload-type node label; schedule onto a pool with
#   nodeSelector: { workload-type: "<key>" }
variable "gpu_pools" {
  type = map(object({
    instance_type = string
    gpu_type      = string
    gpu_count     = optional(number, 1)
    min_nodes     = optional(number, 0)
    max_nodes     = number
    spot          = optional(bool, false)
    disk_size_gb  = optional(number, 200)
    subnet_ids    = optional(list(string))
  }))
  description = "GPU node groups, keyed by pool name. gpu_type is an informational label (e.g. \"nvidia-l4\" for g6 instances); min_nodes 0 = scale-to-zero via the cluster autoscaler; spot switches the capacity type; subnet_ids optionally pins a pool to specific subnets (e.g. AZs with GPU stock)."
  default     = {}
}

# --- Worker identity (IRSA) ---

variable "workload_namespace" {
  type        = string
  description = "Kubernetes namespace of the serving workloads."
  default     = "open-serve"
}

variable "workload_ksa_name" {
  type        = string
  description = "Kubernetes service account name bound to the worker IAM role via IRSA."
  default     = "open-serve-worker"
}

variable "model_bucket_name" {
  type        = string
  description = "S3 bucket with model weights. When set, the worker IAM role gets s3:GetObject/ListBucket on it."
  default     = null
}

variable "create_model_bucket" {
  type        = bool
  description = "Create the model_bucket_name S3 bucket (versioning off, public access blocked). Leave false to bring your own."
  default     = false
}
