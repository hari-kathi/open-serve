#!/usr/bin/env bash
# gcp-preflight.sh — prepare and verify a GCP project for an open-serve deployment.
#
# Checks (and where possible, fixes) everything the Terraform reference in
# terraform/gcp/ and examples/gcp-quickstart/ needs:
#   1. gcloud installed + authenticated
#   2. project exists and is accessible
#   3. billing linked (cannot be fixed non-interactively — prints instructions)
#   4. required APIs enabled (enables any that are missing)
#   5. Application Default Credentials present (needed by Terraform)
#   6. GPU / CPU / SSD quotas in the target region (prints request instructions
#      for anything at zero — quota increases are a manual console step)
#
# Usage:
#   scripts/gcp-preflight.sh <PROJECT_ID> [REGION]
#
# Safe to re-run at any time; enabling an already-enabled API is a no-op.
set -euo pipefail

PROJECT_ID="${1:-}"
REGION="${2:-us-central1}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "usage: $0 <PROJECT_ID> [REGION]" >&2
  exit 2
fi

# APIs required by terraform/gcp/{bootstrap,network,cluster}. Keep this list in
# sync with terraform/gcp/bootstrap/main.tf.
REQUIRED_APIS=(
  container.googleapis.com
  compute.googleapis.com
  monitoring.googleapis.com
  logging.googleapis.com
  cloudresourcemanager.googleapis.com
  servicenetworking.googleapis.com
  secretmanager.googleapis.com
  artifactregistry.googleapis.com
  iam.googleapis.com
)

# Quota metrics worth checking before creating GPU node pools. New projects
# usually start with GPU quota = 0; pools can still be created, but nodes will
# not scale up until quota is granted.
QUOTA_METRICS=(
  CPUS
  SSD_TOTAL_GB
  NVIDIA_L4_GPUS
  PREEMPTIBLE_NVIDIA_L4_GPUS
  NVIDIA_A100_GPUS
  NVIDIA_T4_GPUS
)

PASS=()
ACTION=()

note()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32mOK\033[0m  %s\n' "$*"; PASS+=("$*"); }
todo()  { printf '  \033[33m!!\033[0m  %s\n' "$*"; ACTION+=("$*"); }

echo "== open-serve GCP preflight: project=${PROJECT_ID} region=${REGION} =="

echo "-- gcloud"
if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed. Install the Google Cloud SDK first:" >&2
  echo "  https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi
ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)
if [[ -z "$ACCOUNT" ]]; then
  todo "No active gcloud account. Run: gcloud auth login"
  echo "Cannot continue without authentication."; exit 1
fi
ok "authenticated as ${ACCOUNT}"

echo "-- project"
if ! gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1; then
  todo "Project ${PROJECT_ID} not found or not accessible to ${ACCOUNT}"
  echo "Create it with: gcloud projects create ${PROJECT_ID}"; exit 1
fi
ok "project ${PROJECT_ID} accessible"

echo "-- billing"
BILLING=$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || echo "unknown")
if [[ "$BILLING" == "True" ]]; then
  ok "billing enabled"
else
  todo "Billing is NOT linked. Link it (required before APIs/clusters work):"
  note "  gcloud billing accounts list"
  note "  gcloud billing projects link ${PROJECT_ID} --billing-account=<ACCOUNT_ID>"
fi

echo "-- required APIs"
ENABLED=$(gcloud services list --enabled --project "$PROJECT_ID" --format='value(config.name)' 2>/dev/null)
MISSING=()
for api in "${REQUIRED_APIS[@]}"; do
  if grep -q "^${api}$" <<<"$ENABLED"; then
    ok "$api"
  else
    MISSING+=("$api")
  fi
done
if (( ${#MISSING[@]} > 0 )); then
  echo "  enabling ${#MISSING[@]} missing API(s) — this can take a few minutes..."
  gcloud services enable "${MISSING[@]}" --project "$PROJECT_ID"
  for api in "${MISSING[@]}"; do ok "$api (enabled now)"; done
fi

echo "-- Application Default Credentials (used by Terraform)"
if gcloud auth application-default print-access-token >/dev/null 2>&1; then
  ok "ADC present"
else
  todo "ADC missing. Either run: gcloud auth application-default login"
  note "  or export a short-lived token before terraform/tofu commands:"
  note "  export GOOGLE_OAUTH_ACCESS_TOKEN=\$(gcloud auth print-access-token)"
fi

echo "-- quotas in ${REGION} (GPU quotas start at 0 on new projects)"
QUOTAS_JSON=$(gcloud compute regions describe "$REGION" --project "$PROJECT_ID" --format=json 2>/dev/null || true)
if [[ -z "$QUOTAS_JSON" ]]; then
  todo "Could not read quotas (compute API may still be propagating — rerun in ~2 min)"
else
  for metric in "${QUOTA_METRICS[@]}"; do
    LIMIT=$(python3 -c "
import json, sys
region = json.loads(sys.argv[1])
limits = {q['metric']: q['limit'] for q in region.get('quotas', [])}
print(limits.get(sys.argv[2], 'unknown'))" "$QUOTAS_JSON" "$metric")
    if [[ "$LIMIT" == "0.0" || "$LIMIT" == "0" ]]; then
      todo "${metric} quota is 0 in ${REGION} — request an increase before these nodes can start"
    elif [[ "$LIMIT" == "unknown" ]]; then
      todo "${metric} quota not reported for ${REGION} — check the console"
    else
      ok "${metric}: ${LIMIT}"
    fi
  done
  note "Request GPU quota increases at:"
  note "  https://console.cloud.google.com/iam-admin/quotas?project=${PROJECT_ID}"
  note "  (filter for 'NVIDIA', pick ${REGION}; small requests are often auto-approved in minutes)"
fi

echo
echo "== summary: ${#PASS[@]} checks OK, ${#ACTION[@]} action item(s) =="
if (( ${#ACTION[@]} > 0 )); then
  printf '  - %s\n' "${ACTION[@]}"
  exit 3
fi
echo "Project is ready. Next: examples/gcp-quickstart/ (terraform), then deploy/flux/ or helm install."
