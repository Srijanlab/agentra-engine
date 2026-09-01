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
    # Idempotent: google_metadata_script_runner startup (a manual re-run
    # without a reboot -- e.g. to pick up a metadata-only startup-script
    # change) hits a bare `mount` unconditionally and fails outright with
    # "already mounted" -- confirmed live -- aborting the whole script
    # before any of the docker steps below ever run.
    mountpoint -q "$MOUNT" || mount "$DEVICE" "$MOUNT"
    mkdir -p "$MOUNT/claude" "$MOUNT/agentra-home" "$MOUNT/repos" "$MOUNT/nginx"
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
    gcs() { docker run --rm google/cloud-sdk:slim gcloud secrets versions access latest --secret="$1" 2>/dev/null || true; }

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

    # Group ID of the HOST's docker.sock (not guessed/hardcoded at image-build
    # time -- COS's own dockerd may not match a GID baked into the agentra
    # image). --group-add below grants agentuser (uid 1000, no root, no sudo)
    # access to the bind-mounted socket via this supplementary group instead.
    DOCKER_SOCK_GID="$(stat -c '%g' /var/run/docker.sock)"

    # Persistent bridge network for the blue/green app containers + nginx --
    # distinct from the ephemeral, per-pre-prod-test network
    # deploy_pre_prod_self_hosted creates on demand (agentra-preprod-net,
    # torn down with each test sibling). Idempotent.
    docker network create agentra-app-net 2>/dev/null || true

    # Which color is currently live survives on the persistent disk, not
    # just in a container's own state -- first boot ever (no marker file
    # yet) defaults to blue. A real promotion (agents/deployment.py's
    # promote_prod_self_hosted) updates this file directly via `docker exec`
    # against the running agentra-proxy container; this script only ever
    # reads it, never resets it, so a redeploy here doesn't undo whichever
    # color the last promotion left active.
    ACTIVE_COLOR_FILE="$MOUNT/nginx/active_color"
    if [ ! -f "$ACTIVE_COLOR_FILE" ]; then
      echo blue > "$ACTIVE_COLOR_FILE"
    fi
    ACTIVE_COLOR="$(cat "$ACTIVE_COLOR_FILE")"
    ACTIVE_CONTAINER="agentra-$ACTIVE_COLOR"

    docker pull "$IMAGE"
    # Every redeploy pulls a fresh :staging image without ever removing the
    # previous one -- confirmed live: 18 accumulated images (~15GB, 91%
    # reclaimable) filled the 16GB /var partition solid and failed a
    # redeploy outright ("no space left on device") partway through
    # `docker pull`. -a also clears images with no running container (the
    # just-superseded previous :staging layers), not just dangling ones.
    docker image prune -af 2>/dev/null || true

    # NIM proxy: app -> agentra-nim-nginx -> agentra-nim-proxy ->
    # integrate.api.nvidia.com. Only carries traffic when the dashboard's Model
    # Backend toggle is "nim" (agentra/agents/base.py:_sdk_env sets
    # ANTHROPIC_BASE_URL=$NIM_PROXY_URL only then); harmless to keep running
    # otherwise. Same $IMAGE as the app, different entrypoint. Idempotent.
    docker rm -f agentra-nim-proxy 2>/dev/null || true
    docker run -d --name agentra-nim-proxy --restart=always \
      --network agentra-app-net \
      --entrypoint uvicorn \
      -e NVIDIA_API_KEY="$(gcs agentra-nvidia-api-key)" \
      -e NIM_MODEL="meta/llama-3.2-90b-vision-instruct" \
      "$IMAGE" agentra.proxy.main:app --host 0.0.0.0 --port 8000

    NIM_NGINX_CONF="$MOUNT/nginx/nim.conf"
    cat > "$NIM_NGINX_CONF" <<'NIMNGINXCONF'
    events { worker_connections 1024; }
    http {
        server {
            listen 80;
            location / {
                proxy_pass http://agentra-nim-proxy:8000;
                proxy_set_header Host $host;
                proxy_set_header Connection '';
                proxy_http_version 1.1;
                chunked_transfer_encoding off;
                proxy_buffering off;
                proxy_cache off;
                proxy_read_timeout 300s;
            }
        }
    }
    NIMNGINXCONF
    docker rm -f agentra-nim-nginx 2>/dev/null || true
    docker run -d --name agentra-nim-nginx --restart=always \
      --network agentra-app-net \
      -v "$NIM_NGINX_CONF:/etc/nginx/nginx.conf:ro" \
      nginx:alpine

    docker rm -f "$ACTIVE_CONTAINER" 2>/dev/null || true
    docker run -d --name "$ACTIVE_CONTAINER" --restart=always \
      --network agentra-app-net \
      -v "$MOUNT/claude:/home/agentuser/.claude" \
      -v "$MOUNT/agentra-home:/home/agentuser/.agentra" \
      -v "$MOUNT/repos:/workspace" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      --group-add "$DOCKER_SOCK_GID" \
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
      -e SLACK_BOT_TOKEN="$(gcs agentra-slack-bot-token)" \
      -e NIM_PROXY_URL="http://agentra-nim-nginx" \
      "$IMAGE" serve --port 8080
    # NIM_PROXY_URL is inert unless the dashboard's Model Backend toggle is set
    # to "nim" (agentra/agents/base.py:_sdk_env). Deliberately NOT setting
    # CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY -- for the default "claude"
    # backend, auth comes from the claude-home volume's login session (one-time
    # `docker exec -it "$ACTIVE_CONTAINER" claude auth login --claudeai`
    # over IAP SSH). No -p/--publish: nginx (agentra-proxy, below) reaches
    # this over agentra-app-net by container DNS name, never the host's own
    # network interface.

    # nginx's proxy config: written fresh only if missing (first boot ever).
    # A real promotion regenerates this in place via `docker exec -i
    # agentra-proxy` (see promote_prod_self_hosted) -- this script must NOT
    # clobber that on a routine redeploy, or every redeploy would silently
    # revert production back to whichever color was active when the VM was
    # first provisioned.
    NGINX_CONF="$MOUNT/nginx/default.conf"
    if [ ! -f "$NGINX_CONF" ]; then
      cat > "$NGINX_CONF" <<NGINXCONF
    server {
        listen 8080;
        location / {
            proxy_pass http://$ACTIVE_CONTAINER:8080;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
    NGINXCONF
    fi

    # agentra-proxy is the STABLE network-namespace anchor cloudflared/
    # agentra-trigger-loop/agentra-pubsub-pull all join below, via --network
    # container:agentra-proxy -- this is the actual fix for the fragility
    # that used to bite every "agentra" container swap: those three used to
    # pin to the APP container's ID directly, so forgetting to recreate them
    # alongside a routine redeploy silently broke the scheduler (dead
    # namespace, every tick failing closed). Now only nginx's OWN conf file
    # changes on a routine promotion (via `nginx -s reload`, not a container
    # recreate), so the anchor's ID never moves and those three never need
    # touching again after this one-time topology adoption.
    docker rm -f agentra-proxy 2>/dev/null || true
    docker run -d --name agentra-proxy --restart=always \
      --network agentra-app-net \
      -v "$NGINX_CONF:/etc/nginx/conf.d/default.conf" \
      -v "$ACTIVE_COLOR_FILE:/etc/nginx/active_color" \
      nginx:alpine

    docker rm -f cloudflared 2>/dev/null || true
    docker run -d --name cloudflared --restart=always \
      --network container:agentra-proxy \
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
    docker rm -f agentra-trigger-loop 2>/dev/null || true
    docker run -d --name agentra-trigger-loop --restart=always \
      --network container:agentra-proxy \
      curlimages/curl:latest \
      sh -c 'while true; do curl -s -X POST -H "Content-Type: application/json" -d "{}" http://localhost:8080/trigger/scheduled; sleep 900; done'

    # Replaces pubsub.tf's push subscription: pulls agentra-work-queue-pull
    # directly using the VM's own service account (roles/pubsub.subscriber,
    # pubsub.tf) instead of Cloud Run's OIDC-verified push ingress, which
    # has no equivalent once requests arrive only via the Cloudflare Tunnel.
    docker rm -f agentra-pubsub-pull 2>/dev/null || true
    docker run -d --name agentra-pubsub-pull --restart=always \
      --network container:agentra-proxy \
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
  # The app container is now agentra-blue or agentra-green depending on
  # which color is currently active (see local.startup_script's
  # ACTIVE_COLOR_FILE) -- find it with:
  #   gcloud compute ssh agentra-orchestrator --zone=${var.zone} --tunnel-through-iap -- docker ps --format '{{.Names}}' | grep '^agentra-'
  value = "gcloud compute ssh agentra-orchestrator --zone=${var.zone} --tunnel-through-iap -- docker exec -it agentra-blue claude auth login --claudeai"
}
