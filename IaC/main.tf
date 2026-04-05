terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "project" {
  project_id = var.project_id
}

# ─────────────────────────────────────────────
# 1. Enable Required APIs
# ─────────────────────────────────────────────
resource "google_project_service" "apis" {
  for_each = toset([
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "storage.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "cloudfunctions.googleapis.com",
    "run.googleapis.com",
    "eventarc.googleapis.com",
    "bigquery.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ─────────────────────────────────────────────
# 2. GCS Bucket – PDF Manifest Uploads
# ─────────────────────────────────────────────
resource "google_storage_bucket" "pdf_manifests" {
  name                        = "${var.project_id}-pdf-manifests"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 90 }
    action    { type = "Delete" }
  }

  cors {
    origin          = ["*"]
    method          = ["GET", "POST", "PUT"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }

  depends_on = [google_project_service.apis]
}

# ─────────────────────────────────────────────
# 3. Artifact Registry — Docker repository
# ─────────────────────────────────────────────
resource "google_artifact_registry_repository" "agenticterps" {
  repository_id = "agenticterps"
  format        = "DOCKER"
  location      = var.region
  description   = "AgenticTerps Docker images"

  depends_on = [google_project_service.apis]
}

# ─────────────────────────────────────────────
# Service Account — Service A seed script
# ─────────────────────────────────────────────
resource "google_service_account" "service_a_seed" {
  account_id   = "service-a-seed"
  display_name = "Service A — Bootstrap Seed Script"
  description  = "Used by seed.py to write shipment documents to Firestore."
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "service_a_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.service_a_seed.email}"

  depends_on = [google_service_account.service_a_seed]
}

# ─────────────────────────────────────────────
# Service Account — Service C (Monitoring & Anomaly Agent)
# ─────────────────────────────────────────────
resource "google_service_account" "service_c" {
  account_id   = "service-c-monitoring"
  display_name = "Service C — Monitoring & Anomaly Agent"
  project      = var.project_id
  depends_on   = [google_project_service.apis]
}

# Read shipment documents from Firestore
resource "google_project_iam_member" "service_c_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.service_c.email}"
}

# Publish messages to risk-detected topic
resource "google_pubsub_topic_iam_member" "service_c_risk_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.risk_detected.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.service_c.email}"
}

# Allow Cloud Run to be invoked (for Pub/Sub push auth)
resource "google_project_iam_member" "service_c_pubsub_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.service_c.email}"
}

# Allow Service C SA to pull images from Artifact Registry
resource "google_artifact_registry_repository_iam_member" "service_c_ar_reader" {
  repository = google_artifact_registry_repository.agenticterps.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.service_c.email}"
}

# ─────────────────────────────────────────────
# 4. Firestore Database (Native mode)
# ─────────────────────────────────────────────
#
# deletion_policy = "DELETE" ensures terraform destroy actually deletes
# the database in GCP rather than just removing it from state (ABANDON).
# Note: GCP soft-deletes Firestore databases for 24 hours after deletion,
# so re-creating with the same name requires waiting 24 hours or using
# a different name.
# ─────────────────────────────────────────────
resource "google_firestore_database" "main" {
  project                     = var.project_id
  name                        = "cargo-monitor"
  location_id                 = var.firestore_location
  type                        = "FIRESTORE_NATIVE"
  deletion_policy             = "DELETE"
  delete_protection_state     = "DELETE_PROTECTION_DISABLED"

  depends_on = [google_project_service.apis]
}

# ─────────────────────────────────────────────
# 5. Pub/Sub Topics
# ─────────────────────────────────────────────

# Service B → Service C: live sensor data
resource "google_pubsub_topic" "telemetry_stream" {
  name       = "telemetry-stream"
  depends_on = [google_project_service.apis]
}

# Service C → Service D: anomaly / risk detected
resource "google_pubsub_topic" "risk_detected" {
  name       = "risk-detected"
  depends_on = [google_project_service.apis]
}

# Service D → Service E: human approved the plan
resource "google_pubsub_topic" "execute_actions" {
  name       = "execute-actions"
  depends_on = [google_project_service.apis]
}

# Dead-letter topic (optional but recommended for production)
resource "google_pubsub_topic" "dead_letter" {
  name       = "dead-letter"
  depends_on = [google_project_service.apis]
}

# ─────────────────────────────────────────────
# 6. Pub/Sub Subscriptions
# Update push_endpoint values after each Cloud Run deploy
# ─────────────────────────────────────────────

resource "google_pubsub_subscription" "telemetry_stream_sub" {
  name  = "telemetry-stream-sub"
  topic = google_pubsub_topic.telemetry_stream.name

  ack_deadline_seconds       = 60
  message_retention_duration = "600s"

  push_config {
    push_endpoint = var.service_c_url != "" ? "${var.service_c_url}/pubsub/telemetry" : "https://placeholder.invalid/pubsub/telemetry"
    attributes    = { x-goog-version = "v1" }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }
}

resource "google_pubsub_subscription" "risk_detected_sub" {
  name  = "risk-detected-sub"
  topic = google_pubsub_topic.risk_detected.name

  ack_deadline_seconds       = 120
  message_retention_duration = "600s"

  push_config {
    push_endpoint = var.service_d_url != "" ? "${var.service_d_url}/pubsub/risk" : "https://placeholder.invalid/pubsub/risk"
    attributes    = { x-goog-version = "v1" }
  }

  retry_policy {
    minimum_backoff = "15s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }
}

resource "google_pubsub_subscription" "execute_actions_sub" {
  name  = "execute-actions-sub"
  topic = google_pubsub_topic.execute_actions.name

  ack_deadline_seconds       = 120
  message_retention_duration = "600s"

  push_config {
    push_endpoint = var.service_e_url != "" ? "${var.service_e_url}/pubsub/execute" : "https://placeholder.invalid/pubsub/execute"
    attributes    = { x-goog-version = "v1" }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 5
  }
}

# ─────────────────────────────────────────────
# 7. BigQuery Dataset – Compliance Trail
# ─────────────────────────────────────────────
resource "google_bigquery_dataset" "compliance_trail" {
  dataset_id                 = "compliance_trail"
  location                   = var.region
  delete_contents_on_destroy = true

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "audit_log" {
  dataset_id          = google_bigquery_dataset.compliance_trail.dataset_id
  table_id            = "audit_log"
  deletion_protection = false

  schema = jsonencode([
    { name = "shipment_id", type = "STRING",    mode = "REQUIRED" },
    { name = "event_type",  type = "STRING",    mode = "REQUIRED" },
    { name = "actor",       type = "STRING",    mode = "NULLABLE" },
    { name = "details",     type = "JSON",      mode = "NULLABLE" },
    { name = "timestamp",   type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

# ─────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────
output "service_c_sa_email" {
  value = google_service_account.service_c.email
}

output "artifact_registry_url" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agenticterps.repository_id}"
  description = "Base URL for all Docker image pushes"
}

output "pdf_bucket_name" {
  value = google_storage_bucket.pdf_manifests.name
}