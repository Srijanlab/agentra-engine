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
# TASK-018: /home/agentuser/.agentra (the multi-app registry + inbox) and
# every registered app's repo checkout now live on a GCS FUSE volume mount
# (storage.tf's agentra_data bucket, mounted at /data below) instead of the
# container's ephemeral local disk -- both survive an instance restart.
# AGENTRA_HOME points registry.py at the mount; server.py's clone-on-register
# path (TASK-016) checks repos out under the same mount for the same reason.

resource "google_cloud_run_v2_service" "agentra" {
  name     = "agentra-orchestrator"
  project  = var.project_id
  location = var.region

  deletion_protection = false

  template {
    # GCS FUSE volume mounts require the gen2 execution environment.
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    volumes {
      name = "agentra-data"
      gcs {
        bucket    = google_storage_bucket.agentra_data.name
        read_only = false
      }
    }

    containers {
      name  = "agentra"
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agentra.repository_id}/agentra:${var.image_tag}"
      args  = ["serve"]

      ports {
        container_port = 8080
      }

      volume_mounts {
        name       = "agentra-data"
        mount_path = "/data"
      }

      env {
        name  = "AGENTRA_HOME"
        value = "/data/home"
      }
      env {
        # Deliberately NOT under /data (the GCS FUSE mount): gcsfuse doesn't
        # support chmod, which `git clone` needs (it sets core.filemode on
        # every checkout) -- confirmed live, clone_repo failed with "chmod
        # on .git/config.lock: Operation not permitted" the first time this
        # pointed at the mount. Repo checkouts live on the container's own
        # local disk instead and are NOT expected to survive a restart --
        # registry.get_app_repo() re-clones automatically from repo_url
        # when a checkout is missing (TASK-018), so the actually-durable
        # copy of a project's history is whatever it has pushed to its own
        # git remote, same as it always was. Under agentuser's own home
        # (not e.g. /repos at the container root), same reasoning as
        # Dockerfile's /home/agentuser/.agentra -- agentuser can create
        # subdirectories under its own home freely; it owns nothing at
        # container root, confirmed live (PermissionError: '/repos').
        name  = "AGENTRA_REPOS_ROOT"
        value = "/home/agentuser/repos"
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

    # Cloudflare Tunnel connector, as a sidecar in this same revision --
    # NOT a separate Cloud Run service. Sidecars in one revision share a
    # network namespace, so this reaches the agentra container over plain
    # localhost, never through Cloud Run's own HTTP ingress/IAM-invoker
    # check (see deploy/cloudflare/terraform/tunnel.tf's comment for why
    # that matters: that check is all-or-nothing per service, and making
    # the whole service public would also expose every unauthenticated
    # /apps, /system/*, /trigger/* endpoint, not just the dashboard).
    # Cloudflare Access (configured on that same hostname) is the only
    # thing gating a human's path in.
    containers {
      name  = "cloudflared"
      image = "docker.io/cloudflare/cloudflared:latest"
      args  = ["tunnel", "--no-autoupdate", "run"]
      # No `depends_on` guarantee needed the other way: cloudflared retries
      # its localhost connection on its own until the agentra container is
      # actually accepting connections, same as any reverse proxy would.
      depends_on = ["agentra"]

      env {
        # cloudflared reads its connector token from this env var when no
        # --token flag is given -- keeps the token out of the container's
        # args (visible in `gcloud run services describe`/Terraform state)
        # the way GIT_ASKPASS already keeps GITHUB_TOKEN out of git config.
        name = "TUNNEL_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cloudflare_tunnel_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        # cloudflared defaults to QUIC (UDP) transport, which fails
        # outright on Cloud Run's networking -- confirmed live: "failed to
        # dial to edge with quic: timeout: handshake did not complete in
        # time", connections never established. http2 runs over plain TCP,
        # which Cloud Run's egress handles fine.
        name  = "TUNNEL_TRANSPORT_PROTOCOL"
        value = "http2"
      }

      resources {
        limits = {
          cpu    = "500m"
          memory = "128Mi"
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
