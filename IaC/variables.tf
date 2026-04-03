variable "project_id" {
  description = "Your GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for most resources"
  type        = string
  default     = "us-central1"
}

variable "firestore_location" {
  description = "Firestore multi-region or region location (e.g. nam5 or us-central)"
  type        = string
  default     = "nam5"   # US multi-region – good for hackathons
}