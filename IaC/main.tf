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
# 3. Firestore Database (Native mode)
# ─────────────────────────────────────────────
resource "google_firestore_database" "main" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]
}

# ─────────────────────────────────────────────
# 4. Pub/Sub Topics
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
# 5. Pub/Sub Subscriptions (Push stubs)
#    URL placeholders – update after deploying Cloud Functions
# ─────────────────────────────────────────────

resource "google_pubsub_subscription" "telemetry_stream_sub" {
  name  = "telemetry-stream-sub"
  topic = google_pubsub_topic.telemetry_stream.name

  ack_deadline_seconds       = 60
  message_retention_duration = "600s"

  # Push config: fill in the Cloud Function URL after deployment
  push_config {
    push_endpoint = "https://${var.region}-${var.project_id}.cloudfunctions.net/monitoring-agent"
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
    push_endpoint = "https://${var.region}-${var.project_id}.cloudfunctions.net/orchestrator-agent"
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
    push_endpoint = "https://${var.region}-${var.project_id}.cloudfunctions.net/execution-agent"
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
# 6. BigQuery Dataset – Compliance Trail
# ─────────────────────────────────────────────
resource "google_bigquery_dataset" "compliance_trail" {
  dataset_id                  = "compliance_trail"
  location                    = var.region
  delete_contents_on_destroy  = true

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "audit_log" {
  dataset_id = google_bigquery_dataset.compliance_trail.dataset_id
  table_id   = "audit_log"
  deletion_protection = false

  schema = jsonencode([
    { name = "shipment_id",  type = "STRING",    mode = "REQUIRED" },
    { name = "event_type",   type = "STRING",    mode = "REQUIRED" },
    { name = "actor",        type = "STRING",    mode = "NULLABLE" },
    { name = "details",      type = "JSON",      mode = "NULLABLE" },
    { name = "timestamp",    type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}