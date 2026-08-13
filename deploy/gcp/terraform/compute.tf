# Phase 1 of the Cloud Run -> VM migration (see cloudrun.tf's own comment
# once Phase 2 lands): agentra needs a real interactive login
# (`claude auth login`, not an exported OAuth token) so its specialized
# agents bill against the Pro/Max subscription instead of an API key. Cloud
# Run has no interactive exec into a live instance -- confirmed, there's no
# `gcloud run services exec` equivalent to `docker exec -it` -- so the one
# step this whole flow depends on (a human pasting a code back into a
# running process) has no supported entry point there. A VM gives real SSH.
#
# Also meaningfully cheaper for this specific workload: cloudrun.tf's
# min=max=1 config bills 24/7 at Cloud Run's elastic/burst rates (CPU always
# allocated), which is the worst-fit pricing model for a permanently-pinned
# background worker. An e2-standard-2 running the same 24/7 is roughly a
# third the monthly cost.

variable "zone" {
  description = <<-EOT
    GCP zone for the agentra VM. Deliberately a different region than
    var.region (which stays us-central1 for Firestore/Artifact
    Registry/Pub/Sub/Secret Manager -- none of that needs to move) --
    e2-standard-2 and n2-standard-2 both hit ZONE_RESOURCE_POOL_EXHAUSTED
    across all four us-central1 zones, so this VM lives in a separate
    region/capacity pool instead. Cross-region calls to Firestore/Artifact
    Registry add a small amount of latency, not a functional issue.
  EOT
  type        = string
  default     = "us-east1-b"
}

# Separate from the boot disk deliberately -- the boot disk is COS,
# effectively stateless/re-creatable at any time; this disk is the durable
# equivalent of docker-compose.yml's claude-home/agentra-home named volumes.
# auto_delete=false so it survives even if the instance itself is ever
# recreated (e.g. a future machine_type change that requires replacement).
resource "google_compute_disk" "agentra_data" {
  project = var.project_id
  zone    = var.zone
  name    = "agentra-data"
  type    = "pd-balanced"
  size    = 20

  depends_on = [google_project_service.apis]
}

# IAP-only SSH -- no public IP on the instance at all (see network_interface
# below), so this is the only inbound path, and only from Google's IAP relay
# (35.235.240.0/20 is Google's documented, fixed IAP range), never the
# public internet. `gcloud compute ssh --tunnel-through-iap` is how you
# reach it; regular SSH clients / a public IP were never in the picture.
resource "google_compute_firewall" "agentra_iap_ssh" {
  project       = var.project_id
  name          = "agentra-allow-iap-ssh"
  network       = "default"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["agentra-vm"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  depends_on = [google_project_service.apis]
}

# Startup script runs as root on every boot (COS re-runs startup-script on
# restart, which is what we want -- containers aren't persisted across a
# reboot the way a systemd-managed service would be, so this re-creates
# them). Idempotent: `docker rm -f` before each `docker run` so a reboot or
# a metadata-only re-apply doesn't fail on "container name already in use".
locals {
  startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail

    # Format the data disk only if it's never been formatted (first boot).
    # /mnt/disks is the one part of /mnt that's actually writable on COS --
    # COS's root filesystem is read-only by design (confirmed live: plain
    # /mnt/agentra-data failed with "Read-only file system"), and
    # /mnt/disks specifically is the documented, blessed path for mounting
    # extra persistent disks there.
    DEVICE=/dev/disk/by-id/google-agentra-data
    MOUNT=/mnt/disks/agentra-data
    if ! blkid "$DEVICE" >/dev/null 2>&1; then
      mkfs.ext4 -F "$DEVICE"
    fi
    mkdir -p "$MOUNT"
    mount "$DEVICE" "$MOUNT"
    mkdir -p "$MOUNT/claude" "$MOUNT/agentra-home" "$MOUNT/repos"
    # agentuser inside the container is uid 1000 (Dockerfile's useradd
    # default) -- chown from the host side since these dirs are freshly
    # created root:root by mkdir above.
    chown -R 1000:1000 "$MOUNT/claude" "$MOUNT/agentra-home" "$MOUNT/repos"

    # COS has no gcloud CLI on the host at all (confirmed live: "gcloud:
    # command not found") -- it's a deliberately minimal OS, just Docker
    # plus a toolbox mechanism. Every secret read below runs a throwaway
    # google/cloud-sdk container instead, authenticated the same way any
    # container on this instance is: the metadata server, automatically,
    # no credential file needed.
    gcs() { docker run --rm google/cloud-sdk:slim gcloud secrets versions access latest --secret="$1"; }

    # docker-credential-gcr *is* preinstalled on COS, but it writes its
    # config under $HOME/.docker, and root's $HOME (/root) is on the same
    # read-only filesystem as /mnt -- confirmed live: "mkdir /root/.docker:
    # read-only file system". /var is COS's actual writable, persistent
    # (survives reboots, not just this boot) system partition, so point
    # DOCKER_CONFIG there instead of accepting the /root default.
    export DOCKER_CONFIG=/var/lib/agentra-docker-config
    mkdir -p "$DOCKER_CONFIG"
    docker-credential-gcr configure-docker --registries="${var.region}-docker.pkg.dev"

    IMAGE="${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}/agentra:${var.image_tag}"

    docker rm -f agentra 2>/dev/null || true
    docker pull "$IMAGE"
    docker run -d --name agentra --restart=always \
      -v "$MOUNT/claude:/home/agentuser/.claude" \
      -v "$MOUNT/agentra-home:/home/agentuser/.agentra" \
      -v "$MOUNT/repos:/workspace" \
      -e AGENTRA_FIRESTORE_PROJECT="${var.project_id}" \
      -e AGENTRA_REPOS_ROOT=/home/agentuser/repos \
      -e GIT_AUTHOR_NAME="${var.git_author_name}" \
      -e GIT_AUTHOR_EMAIL="${var.git_author_email}" \
      -e GIT_COMMITTER_NAME="${var.git_author_name}" \
      -e GIT_COMMITTER_EMAIL="${var.git_author_email}" \
      -e GITHUB_TOKEN="$(gcs agentra-github-token)" \
      -e GIT_ASKPASS=/usr/local/bin/git-askpass.sh \
      -e GITHUB_APP_ID="$(gcs agentra-github-app-id)" \
      -e GITHUB_APP_PRIVATE_KEY="$(gcs agentra-github-app-private-key)" \
      -e ALARM_WEBHOOK_PASSWORD="$(gcs agentra-alarm-webhook-password)" \
      "$IMAGE" serve --port 8080
    # Deliberately NOT setting CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY --
    # auth comes from the claude-home volume's login session (one-time
    # `docker exec -it agentra claude auth login --claudeai` over IAP SSH).
    # No -p/--publish: cloudflared reaches this over the shared network
    # namespace below, never the host's own network interface.

    docker rm -f cloudflared 2>/dev/null || true
    docker run -d --name cloudflared --restart=always \
      --network container:agentra \
      -e TUNNEL_TOKEN="$(gcs agentra-cloudflare-tunnel-token)" \
      -e TUNNEL_TRANSPORT_PROTOCOL=http2 \
      cloudflare/cloudflared:latest tunnel --no-autoupdate run

    # Replaces scheduler.tf's Cloud-Run-targeting Cloud Scheduler job:
    # server.py's /trigger/scheduled already no-ops per-app until that
    # app's own schedule_hours has elapsed, so a dumb 15-minute local loop
    # hitting localhost costs nothing extra when nothing is due -- same
    # reasoning the old scheduler.tf comment gave, just local now instead
    # of needing Cloud Scheduler + OIDC to reach it. No "app" in the body
    # -- /trigger/scheduled fans that bare tick out to every app in the
    # registry, not just "agentra" itself (a previous version hardcoded
    # {"app":"agentra"} here, which silently meant only agentra's own
    # objective ever got scheduled -- every other managed app was never
    # ticked at all).
    #
    # IMPORTANT for manual redeploys: this container (and
    # agentra-pubsub-pull below) join agentra's network namespace via
    # --network container:agentra, which pins to that specific container
    # ID at creation time -- if agentra is later removed and recreated
    # (as every manual `docker rm -f agentra && docker run ...` redeploy
    # does) without also recreating this one, it's left attached to a
    # dead namespace and every tick silently fails closed (connection
    # refused, swallowed by curl -s). Always recreate agentra,
    # cloudflared, agentra-trigger-loop, and agentra-pubsub-pull together
    # on every redeploy, not just agentra + cloudflared.
    docker rm -f agentra-trigger-loop 2>/dev/null || true
    docker run -d --name agentra-trigger-loop --restart=always \
      --network container:agentra \
      curlimages/curl:latest \
      sh -c 'while true; do curl -s -X POST -H "Content-Type: application/json" -d "{}" http://localhost:8080/trigger/scheduled; sleep 900; done'

    # Replaces pubsub.tf's push subscription: pulls agentra-work-queue-pull
    # directly using the VM's own service account (roles/pubsub.subscriber,
    # pubsub.tf) instead of Cloud Run's OIDC-verified push ingress, which
    # has no equivalent once requests arrive only via the Cloudflare Tunnel.
    docker rm -f agentra-pubsub-pull 2>/dev/null || true
    docker run -d --name agentra-pubsub-pull --restart=always \
      --network container:agentra \
      google/cloud-sdk:slim \
      bash -c 'while true; do gcloud pubsub subscriptions pull agentra-work-queue-pull --limit=5 --auto-ack --format="value(message.data)" | while read -r line; do [ -n "$line" ] && echo "$line" | base64 -d | curl -s -X POST -H "Content-Type: application/json" -d @- http://localhost:8080/trigger/queue; done; sleep 10; done'
  EOT
}

resource "google_compute_instance" "agentra" {
  project      = var.project_id
  zone         = var.zone
  name         = "agentra-orchestrator"
  # Back to e2-standard-2 (the original, cheaper choice) -- the shortage
  # that forced n2-standard-2 was specific to us-central1; worth retrying
  # the cheaper type fresh in us-east1 before assuming it's constrained
  # there too.
  machine_type = "e2-standard-2"
  tags         = ["agentra-vm"]

  boot_disk {
    initialize_params {
      # Container-Optimized OS -- Docker pre-installed, minimal attack
      # surface, automatic OS patching. No cron, no general package manager
      # by design; that's why the trigger loop above is a container, not a
      # crontab entry.
      image = "cos-cloud/cos-stable"
      size  = 20
    }
  }

  attached_disk {
    source      = google_compute_disk.agentra_data.id
    device_name = "agentra-data"
  }

  network_interface {
    network = "default"
    # Ephemeral external IP, for outbound only -- this VM pulls from Docker
    # Hub (cloudflared/curl/cloud-sdk images, not just Artifact Registry)
    # and talks to api.anthropic.com/api.github.com, none of which are
    # reachable via Private Google Access. The alternative, Cloud NAT, costs
    # ~$32+/month for the gateway alone -- more than this VM's entire
    # compute cost -- for a property (no public IP) that isn't actually
    # buying any security here: the firewall (agentra_iap_ssh, above) is the
    # only thing that determines what's reachable inbound, and it permits
    # nothing except IAP-tunneled SSH regardless of whether this address is
    # public or private. An ephemeral IP with a deny-all-but-IAP firewall is
    # exactly as closed to the internet as no IP at all.
    access_config {}
  }

  metadata = {
    startup-script = local.startup_script
  }

  # Default compute service account -- already granted datastore.user
  # (firestore.tf) and secretmanager.secretAccessor on every secret this
  # startup script reads (secrets.tf); adding pubsub.subscriber there too.
  service_account {
    scopes = ["cloud-platform"]
  }

  depends_on = [google_firestore_database.agentra]
}

output "agentra_vm_ssh_command" {
  value = "gcloud compute ssh agentra-orchestrator --zone=${var.zone} --tunnel-through-iap"
}

output "agentra_vm_login_command" {
  value = "gcloud compute ssh agentra-orchestrator --zone=${var.zone} --tunnel-through-iap -- docker exec -it agentra claude auth login --claudeai"
}
