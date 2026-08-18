# Bootstrap an EXISTING GCP project for open-serve: enable the required APIs
# and (optionally) create the Terraform state and model-weights buckets.
#
# Project creation (org/billing wiring) is deliberately out of scope — most
# users bring a project. If you need one:
#   gcloud projects create <project-id> && gcloud billing projects link ...

resource "google_project_service" "apis" {
  for_each = toset(var.apis)

  project = var.project_id
  service = each.key

  # Keep APIs enabled on destroy so tearing down this module does not break
  # other resources still running in the project.
  disable_on_destroy         = false
  disable_dependent_services = false

  timeouts {
    create = "30m"
    update = "40m"
  }
}

# Optional: versioned bucket for Terraform remote state.
resource "google_storage_bucket" "state" {
  count = var.create_state_bucket ? 1 : 0

  name                        = var.state_bucket_name
  project                     = var.project_id
  location                    = var.bucket_location
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.apis]
}

# Optional: bucket for model weights. Versioning is off — model artifacts are
# large and immutable-by-convention (publish new paths, don't overwrite).
resource "google_storage_bucket" "models" {
  count = var.create_model_bucket ? 1 : 0

  name                        = var.model_bucket_name
  project                     = var.project_id
  location                    = var.bucket_location
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = false
  }

  depends_on = [google_project_service.apis]
}
