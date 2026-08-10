# TASK-011(a): scheduled invocation. Dedicated service account (not the
# default compute one) so this job's Cloud Run invoker grant is scoped to
# exactly what it needs and nothing else -- if this credential ever leaked,
# all it can do is POST to this one service's trigger endpoints.
resource "google_service_account" "scheduler_invoker" {
  project      = var.project_id
  account_id   = "agentra-scheduler-invoker"
  display_name = "Cloud Scheduler -> agentra orchestrator invoker"
}

# Paused, not deleted: there's no app registered yet (deployed idle, on
# purpose -- app registration is planned via an admin UI, not part of this
# task queue), so a live cron would just be dead weight in the logs every
# run. The job, schedule, and payload shape are all real and correct --
# `gcloud scheduler jobs resume agentra-daily-cycle` (or update --app=<name>
# once an app is registered) is the only step needed to turn this into a
# working recurring cycle.
resource "google_cloud_scheduler_job" "daily_cycle" {
  project   = var.project_id
  region    = var.region
  name      = "agentra-daily-cycle"
  schedule  = "0 9 * * *"
  time_zone = "UTC"
  paused    = true

  http_target {
    uri         = "${google_cloud_run_v2_service.agentra.uri}/trigger/scheduled"
    http_method = "POST"
    headers = {
      "Content-Type" = "application/json"
    }
    # Placeholder app name -- update once a real app is registered.
    body = base64encode(jsonencode({ app = "REPLACE_WITH_REGISTERED_APP_NAME" }))

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }

  retry_config {
    retry_count = 1
  }
}

# TASK-019: daily standup, one job for every registered app (no per-app
# placeholder needed -- /standup/daily iterates registry.list_apps() and
# no-ops cleanly with zero registered, same as daily_cycle above). Paused
# for the same reason: deployed idle, on purpose, until an app exists.
resource "google_cloud_scheduler_job" "daily_standup" {
  project   = var.project_id
  region    = var.region
  name      = "agentra-daily-standup"
  schedule  = "0 8 * * *"
  time_zone = "UTC"
  paused    = true

  http_target {
    uri         = "${google_cloud_run_v2_service.agentra.uri}/standup/daily"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.scheduler_invoker.email
    }
  }

  retry_config {
    retry_count = 1
  }
}
