variable "project_id" {
  type        = string
  description = "Existing GCP project to deploy into."
}

variable "region" {
  type        = string
  description = "GCP region for the network and cluster."
  default     = "us-central1"
}
