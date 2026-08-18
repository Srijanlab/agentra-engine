# Self-hosted VM deployment config

Read by `agentra.environments.load_self_hosted_vm_config` when this repo's
`EnvironmentConfig.deploy_strategy` is `"self_hosted_vm"` — the
`agents/deployment.py` strategy functions (`deploy_pre_prod_self_hosted`,
`promote_prod_self_hosted`, `teardown_self_hosted_preprod`) derive every
container/network name and VM identifier from this file rather than
hardcoding agentra's own values, so the same strategy code works for any
repo with a similar self-hosted Docker VM setup — only this file's contents
are agentra-specific, not the code that reads it.

See `deploy/gcp/terraform/compute.tf` for how the VM itself is provisioned,
and `docs/deployment.md` for the manual redeploy/promotion runbook this
config's values match.

```yaml
vm_name: agentra-orchestrator
vm_zone: us-east1-b
gcp_project: agentra-prod
image_repo: us-central1-docker.pkg.dev/agentra-prod/agentra/agentra
anchor_container: agentra-proxy
app_network: agentra-app-net
preprod_network: agentra-preprod-net
data_mount: /mnt/disks/agentra-data
firestore_project: agentra-prod
```
