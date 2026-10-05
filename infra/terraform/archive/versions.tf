terraform {
  required_version = "= 1.15.8"

  backend "gcs" {
    bucket = "motionexpaiweb-880586285913-hk-movie-rag-tfstate"
    prefix = "hk-movie-rag/archive"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 7.41.0"
    }
  }
}

provider "google" {
  project = var.project_id
}
