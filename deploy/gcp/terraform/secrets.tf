# TASK-013: same value that was sitting as a loose, untracked
# .claude_oauth_token file at the repo root -- read once during setup,
# migrated here, and the loose file removed from the working tree.
#
# No longer read by anything after the Cloud Run -> VM migration
# (cloudrun.tf) -- the VM authenticates via an interactive `claude auth
# login` session on its persistent disk instead. Kept declared anyway
# (human call, not an oversight): it's a ready-to-use fallback auth path
# for compute.tf's VM too, exactly like it was for Cloud Run -- add a
# CLAUDE_CODE_OAUTH_TOKEN env block reading this secret to the VM's docker
# run command if the login-session path ever needs a fallback.
resource "google_secret_manager_secret" "claude_code_oauth_token" {
  project   = var.project_id
  secret_id = "agentra-claude-code-oauth-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "claude_code_oauth_token" {
  secret      = google_secret_manager_secret.claude_code_oauth_token.id
  secret_data = var.claude_code_oauth_token

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# Same story as above, for the loose .github_pat file -- used by
# git-askpass.sh (GIT_ASKPASS) for git pull/push in the deployed
# environment (TASK-014).
resource "google_secret_manager_secret" "github_token" {
  project   = var.project_id
  secret_id = "agentra-github-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "github_token" {
  secret      = google_secret_manager_secret.github_token.id
  secret_data = var.github_token

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# Runtime identity (default compute SA -- used by both cloudrun.tf and
# compute.tf, neither sets a dedicated one) needs to read these.
resource "google_secret_manager_secret_iam_member" "claude_token_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.claude_code_oauth_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_secret_manager_secret_iam_member" "github_token_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# TASK-011(b): the Basic Auth password server.py's _verify_alarm_webhook_auth
# checks on /trigger/alarm -- see that function's docstring for why this
# path needs its own credential instead of relying on Cloud Run's IAM
# invoker check like the other two trigger paths do.
resource "random_password" "alarm_webhook_password" {
  length  = 32
  special = false
}

resource "google_secret_manager_secret" "alarm_webhook_password" {
  project   = var.project_id
  secret_id = "agentra-alarm-webhook-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "alarm_webhook_password" {
  secret      = google_secret_manager_secret.alarm_webhook_password.id
  secret_data = random_password.alarm_webhook_password.result
}

resource "google_secret_manager_secret_iam_member" "alarm_webhook_password_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.alarm_webhook_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# The cloudflared sidecar's connector token (deploy/cloudflare/terraform's
# `tunnel_token` output) -- lets the dashboard live at
# https://agentra.srijanlab.com behind Cloudflare Access without ever
# making this Cloud Run service itself publicly invokable. See
# cloudrun.tf's cloudflared container block for why this is a sidecar, not
# a separate service.
resource "google_secret_manager_secret" "cloudflare_tunnel_token" {
  project   = var.project_id
  secret_id = "agentra-cloudflare-tunnel-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "cloudflare_tunnel_token" {
  secret      = google_secret_manager_secret.cloudflare_tunnel_token.id
  secret_data = var.cloudflare_tunnel_token

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "cloudflare_tunnel_token_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.cloudflare_tunnel_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# GitHub App connector (agentra/connectors/github_app.py) -- replaces the
# fine-grained PAT's fixed repo scope with installation tokens minted on
# demand for whatever org/repo the App gets installed on. GITHUB_APP_ID
# isn't secret by itself, but it's meaningless without the private key, so
# both live together here for a single rotation story.
resource "google_secret_manager_secret" "github_app_id" {
  project   = var.project_id
  secret_id = "agentra-github-app-id"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "github_app_id" {
  secret      = google_secret_manager_secret.github_app_id.id
  secret_data = var.github_app_id

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "github_app_id_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_app_id.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_secret_manager_secret" "github_app_private_key" {
  project   = var.project_id
  secret_id = "agentra-github-app-private-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "github_app_private_key" {
  secret      = google_secret_manager_secret.github_app_private_key.id
  secret_data = var.github_app_private_key

  lifecycle {
    ignore_changes = [secret_data]
  }
}

resource "google_secret_manager_secret_iam_member" "github_app_private_key_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_app_private_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# agentra-slack-bot-token was created outside Terraform (no
# google_secret_manager_secret resource for it here), so this grant
# references it by its bare secret_id instead of a resource attribute --
# compute.tf's docker run fetches it as SLACK_BOT_TOKEN for
# connectors/slack.py's notify_shipped/notify_human_input_required.
resource "google_secret_manager_secret_iam_member" "slack_bot_token_access" {
  project   = var.project_id
  secret_id = "agentra-slack-bot-token"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# agentra-nvidia-api-key was also created outside Terraform (its value never
# transits here) -- compute.tf's startup script fetches it as NVIDIA_API_KEY for
# the agentra-nim-proxy container (agentra/proxy/main.py). Same bare-secret_id
# grant pattern as agentra-slack-bot-token above.
resource "google_secret_manager_secret_iam_member" "nvidia_api_key_access" {
  project   = var.project_id
  secret_id = "agentra-nvidia-api-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
