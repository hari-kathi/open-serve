output "cluster_name" {
  description = "Name of the GKE cluster."
  value       = google_container_cluster.this.name
}

output "cluster_id" {
  description = "Fully qualified ID of the GKE cluster."
  value       = google_container_cluster.this.id
}

output "cluster_endpoint" {
  description = "IP address of the cluster control plane."
  value       = google_container_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  description = "Base64-encoded root CA certificate of the cluster."
  value       = google_container_cluster.this.master_auth[0].cluster_ca_certificate
}

output "cluster_location" {
  description = "Location of the cluster."
  value       = google_container_cluster.this.location
}

output "worker_service_account_email" {
  description = "Email of the worker GCP service account (annotate the KSA with this for Workload Identity)."
  value       = google_service_account.worker.email
}

output "system_pool_name" {
  description = "Name of the system node pool."
  value       = google_container_node_pool.system.name
}

output "gpu_pool_names" {
  description = "Map of gpu_pools key => actual node pool name."
  value       = { for k, p in google_container_node_pool.gpu : k => p.name }
}

output "hf_token_secret_id" {
  description = "Secret Manager secret ID for the Hugging Face token (null when not created)."
  value       = var.create_hf_token_secret ? google_secret_manager_secret.hf_token[0].secret_id : null
}
