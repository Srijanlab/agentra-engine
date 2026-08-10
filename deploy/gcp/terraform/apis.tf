# APIs were already manually enabled during initial project setup
# (run.googleapis.com, artifactregistry.googleapis.com,
# secretmanager.googleapis.com, cloudscheduler.googleapis.com,
# pubsub.googleapis.com, cloudbuild.googleapis.com, monitoring.googleapis.com,
# logging.googleapis.com, iam.googleapis.com,
# cloudresourcemanager.googleapis.com). Declaring them here too so a fresh
# `terraform apply` against a brand new project is self-sufficient and
# doesn't silently depend on that manual step having happened first.

locals {
  required_apis = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "pubsub.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.required_apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
