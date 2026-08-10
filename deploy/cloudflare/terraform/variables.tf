variable "cloudflare_account_id" {
  description = "Cloudflare account ID (not zone ID) -- find it in the dashboard sidebar for srijanlab.com, or via `wrangler whoami`."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Zone ID for srijanlab.com (GET /zones?name=srijanlab.com, or the dashboard sidebar -- not the account ID)."
  type        = string
}

variable "protected_domain" {
  description = "The exact hostname to gate behind Access and route through the Tunnel."
  type        = string
  default     = "agentra.srijanlab.com"
}

variable "allowed_emails" {
  description = "Email addresses allowed through via one-time PIN. Everyone else is denied -- Access defaults closed."
  type        = list(string)
  default     = ["rossharma1@gmail.com"]
}

variable "session_duration" {
  description = "How long an authenticated session lasts before re-prompting for login."
  type        = string
  default     = "24h"
}
