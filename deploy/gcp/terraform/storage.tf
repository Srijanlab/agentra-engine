# TASK-018: durable storage for the multi-app registry (~/.agentra/apps.json),
# each registered app's repo checkout, and each app's own .agentra/ memory --
# all previously on Cloud Run's ephemeral local disk, gone on every restart
# (see cloudrun.tf's TASK-012-era comment on this). A Cloud Run GCS FUSE
# volume mount is the simplest fix that needs no new database: the bucket is
# mounted straight into the container's filesystem, so every path already
# written under AGENTRA_HOME (registry.py) or a repo checkout (server.py's
# clone-on-register, TASK-016) just works unmodified, durably, as long as
# those paths resolve under the mount.
resource "google_storage_bucket" "agentra_data" {
  name     = "${var.project_id}-agentra-data"
  project  = var.project_id
  location = var.region

  uniform_bucket_level_access = true

  # This bucket is agentra's own operational state (registry, repo
  # checkouts, memory) -- not a build artifact or log -- so it isn't
  # versioned/lifecycle-managed like Artifact Registry images are.
}

# Cloud Run v2's runtime service account here is the project's default
# compute SA (cloudrun.tf doesn't set a dedicated one):
# {project_number}-compute@developer.gserviceaccount.com
resource "google_storage_bucket_iam_member" "agentra_data_runtime_sa" {
  bucket = google_storage_bucket.agentra_data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
