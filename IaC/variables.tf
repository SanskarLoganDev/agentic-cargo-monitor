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
  description = "Firestore multi-region or region location"
  type        = string
  default     = "nam5"
}

# ── Cloud Run URLs — fill in after each service is deployed ──
# Leave as "" before the service exists. Terraform will use a
# placeholder URL that keeps infra valid but won't receive real traffic.
variable "service_c_url" {
  description = "Cloud Run URL for Service C (monitoring agent)"
  type        = string
  default     = ""
}

variable "service_d_url" {
  description = "Cloud Run URL for Service D (orchestrator agent)"
  type        = string
  default     = ""
}

variable "service_e_url" {
  description = "Cloud Run URL for Service E (execution agent)"
  type        = string
  default     = ""
}