output "cluster_name" {
  description = "Name of the GKE cluster."
  value       = module.cluster.cluster_name
}

output "cluster_location" {
  description = "Location of the GKE cluster."
  value       = module.cluster.cluster_location
}

output "cluster_endpoint" {
  description = "IP address of the cluster control plane."
  value       = module.cluster.cluster_endpoint
}

output "worker_service_account_email" {
  description = "GCP SA for serving pods; annotate the open-serve-worker KSA with this."
  value       = module.cluster.worker_service_account_email
}

output "gpu_pool_names" {
  description = "Provisioned GPU node pool names."
  value       = module.cluster.gpu_pool_names
}

output "get_credentials_command" {
  description = "Command to configure kubectl."
  value       = "gcloud container clusters get-credentials ${module.cluster.cluster_name} --region ${module.cluster.cluster_location} --project ${var.project_id}"
}
