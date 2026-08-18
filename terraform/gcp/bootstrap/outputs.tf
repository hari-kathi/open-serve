output "project_id" {
  description = "The bootstrapped project ID."
  value       = var.project_id
}

output "enabled_services" {
  description = "APIs enabled on the project."
  value       = [for s in google_project_service.apis : s.service]
}

output "state_bucket_name" {
  description = "Name of the Terraform state bucket (null when not created)."
  value       = var.create_state_bucket ? google_storage_bucket.state[0].name : null
}

output "model_bucket_name" {
  description = "Name of the model-weights bucket (null when not created)."
  value       = var.create_model_bucket ? google_storage_bucket.models[0].name : null
}
