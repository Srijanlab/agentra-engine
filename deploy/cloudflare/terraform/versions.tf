terraform {
  required_version = ">= 1.5"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Reads CLOUDFLARE_API_TOKEN from the environment automatically -- no
# credentials in this file. Mirrors ContentAutomationPlatform's
# deploy/cloudflare/terraform/versions.tf pattern (same Cloudflare
# account, same srijanlab.com zone, separate Terraform state per app).
provider "cloudflare" {}
