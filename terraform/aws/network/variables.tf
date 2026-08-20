variable "name" {
  type        = string
  description = "Base name for the network resources (VPC, subnets, NAT, route tables)."
  default     = "open-serve"
}

variable "cluster_name" {
  type        = string
  description = "Name of the EKS cluster that will live in this VPC. Used for the kubernetes.io/cluster/<name>=shared subnet tags that EKS and the AWS load balancer controllers rely on."
  default     = "open-serve"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC. Carved into /20 public and private subnets per AZ."
  default     = "10.20.0.0/16"
}

variable "az_count" {
  type        = number
  description = "Number of availability zones to spread subnets across."
  default     = 3
}

variable "nat_per_az" {
  type        = bool
  description = "One NAT gateway per AZ (HA, ~3x the NAT cost) instead of a single shared gateway. The single-gateway default is the cost-conscious choice for quickstarts."
  default     = false
}

variable "tags" {
  type        = map(string)
  description = "Extra tags applied to every resource in this module."
  default     = {}
}
