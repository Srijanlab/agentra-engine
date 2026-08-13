# PHASE 2 COMPLETE: Cloud Run's google_cloud_run_v2_service.agentra
# (TASK-012) is gone, replaced by compute.tf's VM. Confirmed working
# end-to-end before removal: claude_agent_sdk.query() authenticating via an
# interactive `claude auth login` session (not a token/key) and completing
# a real call, all 4 containers (agentra/cloudflared/trigger-loop/
# pubsub-pull) healthy, Firestore reachable cross-region. Removed promptly
# once confirmed rather than left idle -- its cloudflared sidecar was still
# connected to the same tunnel as the VM's the whole time Cloud Run stayed
# up, so the two were silently racing for the same dashboard traffic.
#
# This file intentionally left without a live resource in it: keeping the
# history/reasoning above rather than deleting the file outright, since
# deploy/gcp/terraform's other files (secrets.tf, firestore.tf) still
# reference "the default compute SA, same identity Cloud Run used" in their
# own comments -- that identity is now compute.tf's VM instead, but the
# grants themselves didn't need to change since it's the same service
# account either way.
