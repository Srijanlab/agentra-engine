# Agentra's own operational state (registry.py: apps registry, kill-switch
# pause marker, inbox requests) -- replaces the earlier GCS-FUSE-mounted-
# JSON-files design (storage.tf, removed). That worked for plain read/write
# but gcsfuse's limited POSIX semantics kept surfacing real bugs (chmod
# failures cloning repos onto the same mount), and Firestore gives proper
# atomic claims for the inbox (transactions) instead of relying on
# filesystem rename semantics on a network filesystem. Project-specific
# data (objective, environments.yaml, architecture notes) stays as
# git-committed files in each app's own repo, deliberately not here -- see
# registry.py's module docstring for why that split is intentional, not an
# oversight.
resource "google_firestore_database" "agentra" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Cloud Run's default compute service account already has broad project
  # access in most default project setups, but grant Firestore access
  # explicitly rather than assume that -- same reasoning as every other
  # secretmanager.secretAccessor grant in this repo.
  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "cloud_run_firestore_access" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
