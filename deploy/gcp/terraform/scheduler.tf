# TASK-011(a): scheduled invocation. Dedicated service account (not the
# default compute one) so this job's Cloud Run invoker grant is scoped to
# exactly what it needs and nothing else -- if this credential ever leaked,
# all it can do is POST to this one service's trigger endpoints.
resource "google_service_account" "scheduler_invoker" {
  project      = var.project_id
  account_id   = "agentra-scheduler-invoker"
  display_name = "Cloud Scheduler -> agentra orchestrator invoker"
}

# Live and targeting agentra itself since the dogfooding directive (was
# deployed paused with a placeholder app name -- resumed and pointed at
# "agentra" once that app was registered, matching what's actually
# running; this source drifted out of sync with that live change until
# now). Hourly, not once-daily: agentra's own EnvironmentConfig.schedule_hours
# (dashboard-configurable, currently 2) is only meaningful if the tick
# checking it fires more often than that -- server.py's /trigger/scheduled
# already no-ops per-app until each app's own schedule_hours has elapsed,
# so ticking hourly costs nothing extra when an app isn't due yet.
# Still agentra-only: other registered apps (e.g. PredictionLeague) have no
# trigger at all yet -- see /trigger/scheduled's single-app payload shape.
resource "google_cloud_scheduler_job" "daily_cycle" {
  project   = var.project_id
  region    = var.region
  name      = "agentra-daily-cycle"
  schedule  = "0 * * * *"
  time_zone = "UTC"
  paused    = false

  http_target {
    uri         = "${google_cloud_run_v2_service.agentra.uri}/trigger/scheduled"
    http_method = "POST"
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({ app = "agentra" }))

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
