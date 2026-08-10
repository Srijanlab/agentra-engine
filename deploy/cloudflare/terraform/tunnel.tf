# A Cloudflare Tunnel into the agentra Cloud Run service -- deliberately NOT
# by making the service publicly invokable (which would also expose
# /trigger/scheduled, /trigger/queue, and every /apps, /system/* endpoint to
# anyone who guesses the *.run.app URL, since Cloud Run's IAM invoker check
# is all-or-nothing per service, not per path -- see cloudrun.tf's own
# comment on this). Instead, `cloudflared` runs as a sidecar container in
# the SAME Cloud Run revision (deploy/gcp/terraform/cloudrun.tf) and reaches
# the app container over localhost within that revision's shared network
# namespace -- never through Cloud Run's own HTTP ingress, so the IAM
# invoker check (still scoped to exactly the scheduler/pubsub service
# accounts) is completely untouched by this. Cloudflare Access (access.tf)
# is the only thing standing between the public internet and the app.

resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "agentra" {
  account_id    = var.cloudflare_account_id
  name          = "agentra-orchestrator"
  tunnel_secret = random_id.tunnel_secret.b64_std
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "agentra" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.agentra.id

  config = {
    ingress = [
      {
        hostname = var.protected_domain
        # cloudflared and the agentra app container share one network
        # namespace as sidecars in the same Cloud Run revision -- this is
        # genuinely localhost, not a public/internal DNS name.
        service = "http://localhost:8080"
      },
      {
        # Cloudflare requires a catch-all final rule with no hostname.
        service = "http_status:404"
      },
    ]
  }
}

# The connector run token `cloudflared tunnel run --token <this>` expects --
# there's no direct computed "token" attribute on the tunnel resource, but
# this is the documented token format (account tag / tunnel id / tunnel
# secret, base64-encoded JSON), constructed from values Terraform already
# has rather than pulled from the dashboard by hand.
locals {
  tunnel_token = base64encode(jsonencode({
    a = var.cloudflare_account_id
    t = cloudflare_zero_trust_tunnel_cloudflared.agentra.id
    s = random_id.tunnel_secret.b64_std
  }))
}
