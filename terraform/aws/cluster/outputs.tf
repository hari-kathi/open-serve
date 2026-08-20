output "cluster_name" {
  description = "Name of the EKS cluster."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "URL of the cluster control-plane endpoint."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64-encoded root CA certificate of the cluster."
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "oidc_provider_arn" {
  description = "ARN of the IAM OIDC provider (the IRSA foundation)."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "node_role_arn" {
  description = "ARN of the IAM role shared by all node groups."
  value       = aws_iam_role.node.arn
}

output "worker_role_arn" {
  description = "ARN of the worker IAM role; annotate the workload KSA with eks.amazonaws.com/role-arn: <this> for IRSA."
  value       = aws_iam_role.worker.arn
}

output "cluster_autoscaler_role_arn" {
  description = "ARN of the cluster-autoscaler IAM role; annotate the kube-system/cluster-autoscaler KSA with it."
  value       = aws_iam_role.cluster_autoscaler.arn
}

output "system_node_group_name" {
  description = "Name of the system node group."
  value       = aws_eks_node_group.system.node_group_name
}

output "gpu_node_group_names" {
  description = "Map of gpu_pools key => node group name."
  value       = { for k, g in aws_eks_node_group.gpu : k => g.node_group_name }
}

output "model_bucket_name" {
  description = "Name of the model bucket (null when not created here)."
  value       = var.create_model_bucket ? aws_s3_bucket.models[0].bucket : null
}

output "update_kubeconfig_command" {
  description = "Command to configure kubectl."
  value       = "aws eks update-kubeconfig --region ${data.aws_region.current.name} --name ${aws_eks_cluster.this.name}"
}
