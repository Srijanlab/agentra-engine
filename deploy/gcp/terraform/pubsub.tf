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

resource "google_service_account" "pubsub_invoker" {
  project      = var.project_id
  account_id   = "agentra-pubsub-invoker"
  display_name = "Pub/Sub push -> agentra orchestrator invoker"
}

# Standard pattern for Pub/Sub -> Cloud Run push auth: the push subscription
# itself needs to be allowed to mint OIDC tokens as this service account.
resource "google_service_account_iam_member" "pubsub_invoker_token_creator" {
  service_account_id = google_service_account.pubsub_invoker.name
  role                = "roles/iam.serviceAccountTokenCreator"
  member              = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "work_queue_push" {
  project = var.project_id
  name    = "agentra-work-queue-push"
  topic   = google_pubsub_topic.work_queue.name

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.agentra.uri}/trigger/queue"
    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
    }
  }

  # Pub/Sub retries on any non-2xx from the push endpoint; trigger_queue()
  # in server.py always returns 200 (even for a malformed/invalid message --
  # see its docstring on why acking-without-processing is correct there), so
  # this ack deadline mainly covers ordinary transient failures (a cold
  # start, a brief Cloud Run hiccup).
  ack_deadline_seconds = 30

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker_pubsub]
}
