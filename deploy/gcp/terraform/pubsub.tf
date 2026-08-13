# TASK-011(c): queue invocation. Anything (a webhook adapter, a future
# admin UI, a support tool) that wants to durably enqueue a bug/feature
# request/objective change for an app publishes a message here instead of
# calling registry.submit_request() directly -- decouples "something wants
# to submit work" from "the orchestrator is up and reachable right now".
#
# Message data (base64-encoded JSON in the Pub/Sub envelope, matching
# registry.submit_request()'s params): {"app": "...", "type": "bug" |
# "feature_request" | "objective_change", "description": "...",
# "severity": "..." (optional), "screenshot_url": "..." (optional)}
resource "google_pubsub_topic" "work_queue" {
  project = var.project_id
  name    = "agentra-work-queue"
}

# PULL, not push (was push+OIDC to Cloud Run's own URI -- see compute.tf's
# top comment for why Cloud Run is going away). Cloud Run's push model
# needed the OIDC dance because *something* had to authenticate an inbound
# request; a VM doesn't have that problem; compute.tf's agentra-pubsub-pull
# container just pulls with the instance's own service account credentials
# and forwards each message to http://localhost:8080/trigger/queue. No
# public ingress needed for this path at all.
resource "google_pubsub_subscription" "work_queue_pull" {
  project = var.project_id
  name    = "agentra-work-queue-pull"
  topic   = google_pubsub_topic.work_queue.name

  ack_deadline_seconds = 30
}

resource "google_project_iam_member" "vm_pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
