variable "project_id" {
  description = "GCP project ID for the agentra deployment."
  type        = string
  default     = "agentra-prod"
}

variable "region" {
  description = "GCP region for all resources."
  type        = string
  default     = "us-central1"
}

variable "artifact_registry_repo" {
  description = "Artifact Registry repo name."
  type        = string
  default     = "agentra"
}

variable "image_tag" {
  description = "Image tag for the agentra orchestrator image."
  type        = string
  default     = "staging"
}

variable "claude_code_oauth_token" {
  description = <<-EOT
    Claude Code OAuth access token (from `claude login`'s accessToken field,
    or the local .claude_oauth_token file). Stored in Secret Manager, never
    as a plain env var or committed to the repo. Expires -- see
    docs/deployment.md for the rotation procedure.

    Not read by anything since the Cloud Run -> VM migration (the VM
    authenticates via an interactive `claude auth login` session on its
    persistent disk instead) -- kept declared deliberately as a ready
    fallback auth path for the VM too, not removed as dead config.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_token" {
  description = <<-EOT
    GitHub PAT with repo scope, used by git-askpass.sh for git pull/push in
    the deployed environment (from the local .github_pat file). Stored in
    Secret Manager.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "cloudflare_tunnel_token" {
  description = <<-EOT
    Cloudflare Tunnel connector token for the agentra-orchestrator tunnel
    (deploy/cloudflare/terraform's `tunnel_token` output). Stored in Secret
    Manager, never as a plain env var or committed to the repo.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_app_id" {
  description = "GitHub App ID for agentra's GitHub connector (agentra/connectors/github_app.py)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_app_private_key" {
  description = "PEM private key for the GitHub App above, from its settings page's 'Generate a private key' button."
  type        = string
  sensitive   = true
  default     = ""
}

variable "git_author_name" {
  type    = string
  default = "agentra-bot"
}

variable "git_author_email" {
  type    = string
  default = "rossharma1@gmail.com"
}
