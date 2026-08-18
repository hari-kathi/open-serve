terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
  }

  # State is local by default (simplest for a quickstart). For anything shared
  # or long-lived, create a state bucket (see the bootstrap module's
  # create_state_bucket) and switch to the GCS backend:
  #
  # backend "gcs" {
  #   bucket = "<your-state-bucket>"
  #   prefix = "open-serve/quickstart"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
