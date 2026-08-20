output "cluster_name" {
  description = "Name of the EKS cluster."
  value       = module.cluster.cluster_name
}

output "cluster_endpoint" {
  description = "URL of the cluster control-plane endpoint."
  value       = module.cluster.cluster_endpoint
}

output "worker_role_arn" {
  description = "IAM role for serving pods; annotate the open-serve-worker KSA with eks.amazonaws.com/role-arn: <this>."
  value       = module.cluster.worker_role_arn
}

output "cluster_autoscaler_role_arn" {
  description = "IAM role for the cluster autoscaler; pass to its Helm chart's serviceAccount annotation."
  value       = module.cluster.cluster_autoscaler_role_arn
}

output "gpu_node_group_names" {
  description = "Provisioned GPU node group names."
  value       = module.cluster.gpu_node_group_names
}

output "update_kubeconfig_command" {
  description = "Command to configure kubectl."
  value       = module.cluster.update_kubeconfig_command
}
