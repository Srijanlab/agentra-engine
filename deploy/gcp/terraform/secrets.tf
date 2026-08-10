# TASK-013: same value that was sitting as a loose, untracked
# .claude_oauth_token file at the repo root -- read once during setup,
# migrated here, and the loose file removed from the working tree. See
# docs/deployment.md for the token rotation procedure (it expires).
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
}

# Runtime identity (default compute SA -- cloudrun.tf doesn't set a
# dedicated one) needs to read both secrets.
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
}

resource "google_secret_manager_secret_iam_member" "cloudflare_tunnel_token_access" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.cloudflare_tunnel_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
