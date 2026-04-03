output "pdf_bucket_name" {
  value       = google_storage_bucket.pdf_manifests.name
  description = "GCS bucket – upload PDFs here"
}

output "firestore_database" {
  value       = google_firestore_database.main.name
  description = "Firestore database name"
}

output "pubsub_topics" {
  value = {
    telemetry_stream = google_pubsub_topic.telemetry_stream.id
    risk_detected    = google_pubsub_topic.risk_detected.id
    execute_actions  = google_pubsub_topic.execute_actions.id
    dead_letter      = google_pubsub_topic.dead_letter.id
  }
}

output "bigquery_dataset" {
  value = google_bigquery_dataset.compliance_trail.dataset_id
}