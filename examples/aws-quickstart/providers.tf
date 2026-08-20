terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0"
    }
  }

  # State is local by default (simplest for a quickstart). For anything shared
  # or long-lived, create an S3 bucket for state and switch to the S3 backend:
  #
  # backend "s3" {
  #   bucket       = "<your-state-bucket>"
  #   key          = "open-serve/quickstart/terraform.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region  = var.region
  profile = var.profile
}
