# Minimal open-serve environment: VPC/NAT -> EKS with a system node group and
# a scale-to-zero spot L4 GPU node group (plus a commented on-demand variant).

module "network" {
  source = "../../terraform/aws/network"

  name         = var.cluster_name
  cluster_name = var.cluster_name
}

module "cluster" {
  source = "../../terraform/aws/cluster"

  name       = var.cluster_name
  subnet_ids = module.network.private_subnet_ids

  # System node group: cluster services (gateway, controllers, KubeRay
  # operator, cluster autoscaler).
  system_instance_type = "m6i.large"
  system_min_nodes     = 1
  system_max_nodes     = 3

  gpu_pools = {
    # Spot L4: cheap single-GPU serving; pods must tolerate interruption.
    # Launches count against the "All G and VT Spot Instance Requests" vCPU
    # quota (L-3819A6DF) — 0 on new accounts; run scripts/aws-preflight.sh.
    gpu-l4-1x = {
      instance_type = "g6.xlarge"
      gpu_type      = "nvidia-l4"
      gpu_count     = 1
      min_nodes     = 0
      max_nodes     = 2
      spot          = true
    }

    # On-demand variant: immune to spot interruption and stockouts, ~2-3x the
    # price. Counts against the "Running On-Demand G and VT instances" vCPU
    # quota (L-DB2E81BA) instead.
    # gpu-l4-1x-od = {
    #   instance_type = "g6.xlarge"
    #   gpu_type      = "nvidia-l4"
    #   gpu_count     = 1
    #   min_nodes     = 0
    #   max_nodes     = 2
    #   spot          = false
    # }
  }
}
