# Cloudflare Tunnel + Access — agentra.srijanlab.com

Puts the agentra dashboard behind `https://agentra.srijanlab.com`, gated by
Cloudflare Access (email one-time-PIN, restricted to
`rossharma1@gmail.com` by default), without ever making the Cloud Run
service itself publicly invokable. Mirrors
ContentAutomationPlatform's `deploy/cloudflare/terraform/access.tf`
pattern, plus a Tunnel (CAP doesn't need one in Terraform -- its tunnel is
a docker-compose sidecar set up by hand; agentra's is Terraform-managed
end to end since there's no persistent host to click through a dashboard
flow on).

## Why a Tunnel, not just `allUsers` invoker

Cloud Run's IAM invoker check is all-or-nothing per service, not per path
-- making the service public would also expose `/trigger/scheduled`,
`/trigger/queue`, and every `/apps`, `/system/*` endpoint (none of which
have their own auth yet) to anyone who finds the `*.run.app` URL, not just
the dashboard. Instead, `cloudflared` runs as a sidecar container in the
same Cloud Run revision (`deploy/gcp/terraform/cloudrun.tf`) and reaches
the app over `localhost` within that revision's shared network namespace
-- never through Cloud Run's own HTTP ingress. The `*.run.app` URL stays
exactly as locked down as it already was (Scheduler/Pub/Sub OIDC only);
Cloudflare Access is the only way in for a human.

## Setup

1. `deploy/cloudflare/.env` already has a working `CLOUDFLARE_API_TOKEN`
   (reused from ContentAutomationPlatform's own token for the same
   account/zone). If tunnel creation fails on a permissions error, the
   token needs `Account > Cloudflare Tunnel > Edit` added at
   https://dash.cloudflare.com/profile/api-tokens.
2. `terraform.tfvars` already has the account ID and zone ID for
   srijanlab.com filled in.
3. Run:

```bash
cd deploy/cloudflare/terraform
export $(grep -v '^#' ../.env | xargs)   # loads CLOUDFLARE_API_TOKEN
terraform init
terraform plan
terraform apply
```

4. Feed the resulting `tunnel_token` output into
   `deploy/gcp/terraform`'s `cloudflare_tunnel_token` variable (Secret
   Manager, wired into the `cloudflared` sidecar container in
   `cloudrun.tf`) and apply that too -- see this repo's top-level
   `docs/deployment.md`.

## Verifying

Visit `https://agentra.srijanlab.com` from a private/incognito window --
you should hit Cloudflare's Access login page, not the dashboard directly.
Enter `rossharma1@gmail.com`, get the emailed PIN, and you should land on
the actual dashboard.

## Adding more allowed users later

Edit `allowed_emails` in `terraform.tfvars`, re-apply.

## Rotating the tunnel secret

`terraform apply -replace=random_id.tunnel_secret` generates a new secret
and a new connector token -- re-run the GCP-side apply afterward with the
new `tunnel_token` output so the deployed sidecar picks it up.
