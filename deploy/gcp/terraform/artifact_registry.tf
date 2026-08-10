resource "google_artifact_registry_repository" "agentra" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}
