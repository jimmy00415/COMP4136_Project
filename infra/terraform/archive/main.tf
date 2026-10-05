resource "google_storage_bucket" "source_archive" {
  name                        = "${var.project_id}-${var.project_number}-hk-movie-rag-source-archive"
  project                     = var.project_id
  location                    = "US-CENTRAL1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "archive_writer" {
  account_id   = "hk-rag-archive-writer"
  display_name = "HK Movie RAG archive writer"
  project      = var.project_id
}

resource "google_storage_bucket_iam_policy" "archive_writer" {
  bucket = google_storage_bucket.source_archive.name
  policy_data = jsonencode({
    bindings = [
      {
        role    = "roles/storage.objectCreator"
        members = ["serviceAccount:${google_service_account.archive_writer.email}"]
      },
      {
        role    = "roles/storage.objectViewer"
        members = ["serviceAccount:${google_service_account.archive_writer.email}"]
      }
    ]
  })
}
