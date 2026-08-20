variable "region" {
  type        = string
  description = "AWS region for the network and cluster."
  default     = "us-east-1"
}

variable "cluster_name" {
  type        = string
  description = "Name of the EKS cluster (also the base name for the network)."
  default     = "open-serve"
}

variable "profile" {
  type        = string
  description = "AWS CLI profile to authenticate with (null uses the default credential chain / $AWS_PROFILE)."
  default     = null
}
