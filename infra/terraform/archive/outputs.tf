output "archive_bucket_name" {
  description = "Name of the private source archive bucket."
  value       = google_storage_bucket.source_archive.name
}

output "archive_writer_service_account" {
  description = "Email address of the archive writer service account."
  value       = google_service_account.archive_writer.email
}
