variable "project_id" {
  type        = string
  description = "ID of an existing GCP project to bootstrap. This module does not create projects."
}

variable "apis" {
  type        = list(string)
  description = "Google APIs to enable on the project."
  default = [
    "container.googleapis.com",
    "compute.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
  ]
}

variable "create_state_bucket" {
  type        = bool
  description = "Create a versioned GCS bucket for Terraform remote state."
  default     = false
}

variable "state_bucket_name" {
  type        = string
  description = "Name for the Terraform state bucket. Required when create_state_bucket is true. Bucket names are globally unique."
  default     = null
}

variable "create_model_bucket" {
  type        = bool
  description = "Create a GCS bucket for model weights."
  default     = false
}

variable "model_bucket_name" {
  type        = string
  description = "Name for the model-weights bucket. Required when create_model_bucket is true. Bucket names are globally unique."
  default     = null
}

variable "bucket_location" {
  type        = string
  description = "Location for created buckets (region or multi-region)."
  default     = "US"
}
