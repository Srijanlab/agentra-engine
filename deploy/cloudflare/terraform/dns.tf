resource "cloudflare_dns_record" "agentra" {
  zone_id = var.cloudflare_zone_id
  name    = var.protected_domain
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.agentra.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1 # "automatic" -- required by the API when proxied = true
}
