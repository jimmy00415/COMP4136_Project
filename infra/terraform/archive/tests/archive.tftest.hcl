mock_provider "google" {}

run "archive_policy" {
  command = apply

  variables {
    project_id     = "motionexpaiweb"
    project_number = "880586285913"
  }

  assert {
    condition     = google_storage_bucket.source_archive.name == "motionexpaiweb-880586285913-hk-movie-rag-source-archive"
    error_message = "The archive bucket name must bind the project ID and project number."
  }

  assert {
    condition     = google_storage_bucket.source_archive.project == "motionexpaiweb"
    error_message = "The archive bucket must belong to the selected project."
  }

  assert {
    condition     = google_storage_bucket.source_archive.location == "US-CENTRAL1"
    error_message = "The archive bucket must use US-CENTRAL1."
  }

  assert {
    condition     = google_storage_bucket.source_archive.uniform_bucket_level_access
    error_message = "Uniform bucket-level access must be enabled."
  }

  assert {
    condition     = google_storage_bucket.source_archive.public_access_prevention == "enforced"
    error_message = "Public access prevention must be enforced."
  }

  assert {
    condition     = google_storage_bucket.source_archive.force_destroy == false
    error_message = "The archive bucket must not allow force-destroy."
  }

  assert {
    condition     = google_storage_bucket.source_archive.versioning[0].enabled
    error_message = "Object versioning must be enabled."
  }

  assert {
    condition     = google_storage_bucket.source_archive.soft_delete_policy[0].retention_duration_seconds == 604800
    error_message = "Soft delete must retain objects for exactly seven days."
  }

  assert {
    condition     = google_service_account.archive_writer.account_id == "hk-rag-archive-writer"
    error_message = "The archive writer service account ID is part of the Task 9 interface."
  }

  assert {
    condition     = google_service_account.archive_writer.project == "motionexpaiweb"
    error_message = "The archive writer must belong to the selected project."
  }

  assert {
    condition     = google_storage_bucket_iam_policy.archive_writer.bucket == google_storage_bucket.source_archive.name
    error_message = "The archive bucket IAM must be managed by one authoritative bucket policy."
  }

  assert {
    condition = (
      length(keys(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data))) == 1 &&
      length(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings) == 2 &&
      length(keys(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[0])) == 2 &&
      jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[0].role == "roles/storage.objectCreator" &&
      length(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[0].members) == 1 &&
      jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[0].members[0] == "serviceAccount:${google_service_account.archive_writer.email}" &&
      length(keys(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[1])) == 2 &&
      jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[1].role == "roles/storage.objectViewer" &&
      length(jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[1].members) == 1 &&
      jsondecode(google_storage_bucket_iam_policy.archive_writer.policy_data).bindings[1].members[0] == "serviceAccount:${google_service_account.archive_writer.email}"
    )
    error_message = "The authoritative bucket policy must contain exactly objectCreator and objectViewer for the writer, with no convenience, public, owner, or editor grant."
  }

  assert {
    condition     = output.archive_bucket_name == google_storage_bucket.source_archive.name
    error_message = "The archive bucket output must expose the provisioned bucket name."
  }

  assert {
    condition     = output.archive_writer_service_account == google_service_account.archive_writer.email
    error_message = "The archive writer output must expose the service account email."
  }
}
