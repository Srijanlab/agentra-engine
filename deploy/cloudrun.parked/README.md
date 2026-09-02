# PARKED — Cloud Run is not usable

GCP billing on `agentra-prod` was closed 2026-09-02. Cloud Run has no
billing-free tier (its "free tier" is an allowance on a billed account), so:

- `agentra-engine` / `agentra-engine-preprod` Cloud Run services -> 503
- Cloud Build + Artifact Registry -> PERMISSION_DENIED

Images are now built by `.github/workflows/image.yml` and pushed to GHCR
(`ghcr.io/srijanlab/agentra-engine`). The compute host is TBD (off GCP).

**Still on GCP, still free (Spark, no billing):** Firestore + Secret Manager.
Off-GCP access to those needs an explicit service-account key for
`agentra-engine-run@agentra-prod.iam.gserviceaccount.com` (has roles/datastore.user):

    gcloud iam service-accounts keys create engine-fs-key.json \
      --iam-account agentra-engine-run@agentra-prod.iam.gserviceaccount.com

`setup.sh` and the old deploy pipeline in the git history created SAs + a WIF
pool that are now unused. If billing ever comes back, `git show` the deleted
`deploy.yml` / this dir's prior `README.md` + `setup.sh`.
