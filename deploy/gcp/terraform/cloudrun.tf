# TASK-012: the always-on orchestrator (agentra/server.py, via `agentra
# serve`). Specialized agents still run on demand as short-lived
# subprocesses the Claude Agent SDK spawns inside this same service process
# (agents/base.py::run_agent) -- that was already "on demand, not a
# standing service" architecturally before this deployment existed; this
# just gives it inbound HTTP paths (Cloud Scheduler / Monitoring alerting /
# Pub/Sub -- scheduler.tf, pubsub.tf) to be triggered from instead of only
# a terminal.
#
# min=max=1 instance, deliberately not autoscaled: server.py's per-app
# duplicate-trigger protection (_app_locks) is in-process state. Multiple
# concurrent instances would each keep their own lock, defeating that
# protection -- a second instance could start a second concurrent cycle for
# an app the first instance already has locked. Fine at this scale (this is
# an internal orchestrator, not a public-traffic service); revisit if
# multi-instance is ever actually needed (would require moving that lock to
# something shared, e.g. a Firestore/Redis-backed lock).
#
# CONTAINER.md's local/CLI-worker hardening (--read-only root fs, --cap-drop
# ALL, tmpfs /tmp) has no direct Cloud Run Terraform equivalent -- Cloud Run
# already runs every instance in its own gVisor sandbox by default, which is
# a stronger isolation boundary than those docker-run flags approximate
# locally. Not attempting to replicate flag-for-flag here for that reason.
#
# Known limitation, not solved here: /home/agentuser/.agentra (the
# multi-app registry + inbox) is on the container's ephemeral local disk --
# it does not survive an instance restart. Deployed idle (no apps
# registered) this doesn't matter yet; once real app registration exists
# (planned: an admin UI, not part of this task queue), that registry needs
# to move to something durable (Firestore, or a Filestore/GCS FUSE mount)
# before it's relied upon in practice.

resource "google_cloud_run_v2_service" "agentra" {
  name     = "agentra-orchestrator"
  project  = var.project_id
  location = var.region

  deletion_protection = false

  template {
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agentra.repository_id}/agentra:${var.image_tag}"
      args  = ["serve"]

      ports {
        container_port = 8080
      }

      env {
        name = "CLAUDE_CODE_OAUTH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.claude_code_oauth_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GITHUB_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.github_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        # git-askpass.sh echoes this for git's password prompt (TASK-014) --
        # keeps the token out of .git/config entirely.
        name  = "GIT_ASKPASS"
        value = "/usr/local/bin/git-askpass.sh"
      }
      env {
        # server.py's _verify_alarm_webhook_auth -- see secrets.tf's
        # alarm_webhook_password resource.
        name = "ALARM_WEBHOOK_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.alarm_webhook_password.secret_id
            version = "latest"
          }
        }
      }
      env {
        name  = "GIT_AUTHOR_NAME"
        value = var.git_author_name
      }
      env {
        name  = "GIT_AUTHOR_EMAIL"
        value = var.git_author_email
      }
      env {
        name  = "GIT_COMMITTER_NAME"
        value = var.git_author_name
      }
      env {
        name  = "GIT_COMMITTER_EMAIL"
        value = var.git_author_email
      }

      resources {
        limits = {
          cpu    = "2000m"
          memory = "2Gi"
        }
      }
    }

    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
  }
}

# Publicly reachable so Cloud Scheduler / Pub/Sub push / a Monitoring
# alerting webhook can all reach it directly. All three (scheduler.tf,
# pubsub.tf) authenticate their own requests via OIDC/IAM at the Cloud Run
# layer instead (roles/run.invoker granted to each trigger's own service
# account below), so "publicly routable" here doesn't mean "unauthenticated
# -- every trigger endpoint still requires a valid Cloud Run invoker
# identity except where explicitly granted to allUsers, which nothing here
# does.
resource "google_cloud_run_v2_service_iam_member" "invoker_scheduler" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agentra.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloud_run_v2_service_iam_member" "invoker_pubsub" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agentra.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

output "orchestrator_url" {
  value = google_cloud_run_v2_service.agentra.uri
}
