# Gates agentra.srijanlab.com behind Cloudflare Access. Nobody reaches the
# dashboard without first passing an email one-time-PIN check against
# var.allowed_emails -- Access defaults to deny for anyone/anything not
# explicitly matched by an "allow" policy, so there's no separate "deny
# everyone else" rule needed. Mirrors ContentAutomationPlatform's
# deploy/cloudflare/terraform/access.tf pattern exactly.
#
# allowed_idps is deliberately left unset -- Cloudflare's built-in
# "One-Time PIN" identity provider is enabled by default on every Zero
# Trust account unless explicitly removed, so this doesn't need a
# provider ID looked up ahead of time.

resource "cloudflare_zero_trust_access_application" "agentra_app" {
  account_id       = var.cloudflare_account_id
  name             = "agentra (${var.protected_domain})"
  domain           = var.protected_domain
  type             = "self_hosted"
  session_duration = var.session_duration

  policies = [
    {
      name       = "Allow owner only"
      precedence = 1
      decision   = "allow"
      include = [
        for email in var.allowed_emails : {
          email = { email = email }
        }
      ]
    }
  ]
}
