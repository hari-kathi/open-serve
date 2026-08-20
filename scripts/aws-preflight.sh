#!/usr/bin/env bash
# aws-preflight.sh — prepare and verify an AWS account for an open-serve deployment.
#
# Checks (and where possible, fixes) everything the Terraform reference in
# terraform/aws/ and examples/aws-quickstart/ needs:
#   1. aws CLI installed + credentials valid
#   2. the two GPU vCPU quotas (both default to 0 on new accounts and are
#      invisible until you look — request increases and poll them here):
#        - Running On-Demand G and VT instances  (L-DB2E81BA)
#        - All G and VT Spot Instance Requests   (L-3819A6DF)
#   3. GPU instance-type availability in the target region's AZs
#
# Usage:
#   scripts/aws-preflight.sh [REGION] [--request N]
#     REGION       defaults to us-east-1 (or $AWS_REGION)
#     --request N  file increase requests for both quotas to N vCPUs
#
# Authenticate first (e.g. `aws configure --profile open-serve` and
# `export AWS_PROFILE=open-serve`). Safe to re-run at any time.
set -euo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
REQUEST_VALUE=""
if [[ "${2:-}" == "--request" ]]; then REQUEST_VALUE="${3:?--request needs a value}"; fi

ONDEMAND_QUOTA=L-DB2E81BA   # Running On-Demand G and VT instances (vCPUs)
SPOT_QUOTA=L-3819A6DF       # All G and VT Spot Instance Requests (vCPUs)
GPU_INSTANCE_TYPE="${GPU_INSTANCE_TYPE:-g6.xlarge}"   # 1x NVIDIA L4, 4 vCPUs

PASS=(); ACTION=()
note()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32mOK\033[0m  %s\n' "$*"; PASS+=("$*"); }
todo()  { printf '  \033[33m!!\033[0m  %s\n' "$*"; ACTION+=("$*"); }

echo "== open-serve AWS preflight: region=${REGION} profile=${AWS_PROFILE:-default} =="

echo "-- aws CLI + credentials"
if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is not installed: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html" >&2
  exit 1
fi
IDENTITY=$(aws sts get-caller-identity --output json 2>/dev/null || true)
if [[ -z "$IDENTITY" ]]; then
  todo "No valid credentials. Run: aws configure --profile <name> && export AWS_PROFILE=<name>"
  exit 1
fi
ACCOUNT=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['Account'])" "$IDENTITY")
ARN=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['Arn'])" "$IDENTITY")
ok "authenticated as ${ARN} (account ${ACCOUNT})"

echo "-- GPU vCPU quotas in ${REGION} (new accounts start at 0 for both)"
for pair in "${ONDEMAND_QUOTA}:On-Demand G and VT" "${SPOT_QUOTA}:Spot G and VT"; do
  code="${pair%%:*}"; label="${pair#*:}"
  VALUE=$(aws service-quotas get-service-quota --service-code ec2 --quota-code "$code" \
    --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "unknown")
  if [[ "$VALUE" == "0.0" || "$VALUE" == "0" ]]; then
    todo "${label} vCPU quota is 0 — no GPU instances of that class can launch"
  elif [[ "$VALUE" == "unknown" ]]; then
    todo "Could not read the ${label} quota (check permissions: servicequotas:GetServiceQuota)"
  else
    ok "${label}: ${VALUE} vCPUs"
  fi
  # Show any in-flight request for this quota
  aws service-quotas list-requested-service-quota-change-history-by-quota \
    --service-code ec2 --quota-code "$code" --region "$REGION" \
    --query 'RequestedQuotas[?Status==`PENDING` || Status==`CASE_OPENED`].[DesiredValue,Status]' \
    --output text 2>/dev/null | while read -r want status; do
      [[ -n "${want:-}" ]] && note "in-flight request: ${label} -> ${want} (${status})"
    done
  if [[ -n "$REQUEST_VALUE" ]]; then
    STATUS=$(aws service-quotas request-service-quota-increase --service-code ec2 \
      --quota-code "$code" --desired-value "$REQUEST_VALUE" --region "$REGION" \
      --query 'RequestedQuota.Status' --output text 2>&1 || true)
    note "filed increase request: ${label} -> ${REQUEST_VALUE} (${STATUS})"
  fi
done
note "Approval is on AWS's timeline — minutes (auto) to days (manual review of"
note "young accounts). Track: aws service-quotas list-requested-service-quota-change-history --service-code ec2"

echo "-- ${GPU_INSTANCE_TYPE} availability in ${REGION}"
AZS=$(aws ec2 describe-instance-type-offerings --location-type availability-zone \
  --filters "Name=instance-type,Values=${GPU_INSTANCE_TYPE}" --region "$REGION" \
  --query 'InstanceTypeOfferings[].Location' --output text 2>/dev/null | tr '\t' ' ' || true)
if [[ -z "$AZS" ]]; then
  todo "${GPU_INSTANCE_TYPE} is not offered in ${REGION} — pick another region or type"
else
  ok "${GPU_INSTANCE_TYPE} offered in: ${AZS}"
fi

echo
echo "== summary: ${#PASS[@]} checks OK, ${#ACTION[@]} action item(s) =="
if (( ${#ACTION[@]} > 0 )); then
  printf '  - %s\n' "${ACTION[@]}"
  exit 3
fi
echo "Account is ready. Next: examples/aws-quickstart/ (terraform), then the software layer."
