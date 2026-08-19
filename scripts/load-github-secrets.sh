#!/bin/bash
# Load GitHub App credentials from GCP Secret Manager for local development

set -e

echo "Loading GitHub App credentials from GCP Secret Manager..."

export GITHUB_APP_ID=$(gcloud secrets versions access latest --secret=agentra-github-app-id --project=agentra-prod)
export GITHUB_APP_PRIVATE_KEY=$(gcloud secrets versions access latest --secret=agentra-github-app-private-key --project=agentra-prod)

echo "✓ GITHUB_APP_ID: $GITHUB_APP_ID"
echo "✓ GITHUB_APP_PRIVATE_KEY: [loaded]"
echo ""
echo "Environment variables exported. Now run:"
echo "  agentra serve"
