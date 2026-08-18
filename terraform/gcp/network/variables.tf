variable "name" {
  type        = string
  description = "Base name for the network resources (VPC, subnet, router, NAT)."
  default     = "open-serve"
}

variable "project_id" {
  type        = string
  description = "GCP project in which to create the network."
}

variable "region" {
  type        = string
  description = "Region for the subnet, router, and NAT."
  default     = "us-central1"
}

variable "subnet_cidr_range" {
  type        = string
  description = "Primary CIDR range for the subnet (GKE nodes)."
  default     = "10.10.0.0/20"
}

variable "pods_cidr_range" {
  type        = string
  description = "Secondary CIDR range for GKE pods."
  default     = "10.12.0.0/16"
}

variable "services_cidr_range" {
  type        = string
  description = "Secondary CIDR range for GKE services."
  default     = "10.13.0.0/20"
}

variable "enable_flow_logs" {
  type        = bool
  description = "Enable VPC flow logs on the subnet (adds logging cost)."
  default     = false
}

variable "nat_ip_count" {
  type        = number
  description = "Number of static NAT IPs to reserve. Set to 0 to let Cloud NAT auto-allocate ephemeral IPs."
  default     = 1
}
