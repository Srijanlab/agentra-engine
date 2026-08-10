output "tunnel_id" {
  value = cloudflare_zero_trust_tunnel_cloudflared.agentra.id
}

output "tunnel_token" {
  value     = local.tunnel_token
  sensitive = true
}

output "access_application_id" {
  value = cloudflare_zero_trust_access_application.agentra_app.id
}

output "access_application_aud" {
  description = "Application Audience (AUD) tag -- needed if the app itself ever validates Access JWTs."
  value       = cloudflare_zero_trust_access_application.agentra_app.aud
}
