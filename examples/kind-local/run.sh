#!/usr/bin/env bash
# open-serve end-to-end example on a local kind cluster.
#
# Builds the service images from this repo, creates a kind cluster, installs
# the KubeRay operator and the open-serve chart with a CPU-only `custom`
# echo model (examples/kind-local/values.yaml), then asserts through the
# gateway that auth + routing + serving actually work.
#
# Idempotent: safe to re-run; it reuses the existing cluster/release/API key.
# Tear down with ./cleanup.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

CLUSTER_NAME="open-serve"
NAMESPACE="open-serve"
KUBERAY_CHART_VERSION="1.6.2"
LOCAL_PORT="18080"

# Match the Ray image to the host architecture (the chart values default to
# the arm64 tag; override for amd64 hosts).
HOST_ARCH="$(uname -m)"
if [[ "${HOST_ARCH}" == "arm64" || "${HOST_ARCH}" == "aarch64" ]]; then
  RAY_IMAGE="rayproject/ray:2.53.0-py311-aarch64"
  EXTRA_HELM_ARGS=()
else
  RAY_IMAGE="rayproject/ray:2.53.0-py311"
  EXTRA_HELM_ARGS=(--set image.tag=2.53.0-py311)
fi

log() { printf '\n==> %s\n' "$*"; }

# --- 1. Build service images (validates all three Dockerfiles; only the
# --- gateway is deployed on kind) -------------------------------------------
log "Building service images"
docker build "${REPO_ROOT}/services/gateway" -t open-serve-gateway:dev
docker build "${REPO_ROOT}/services/probe" -t open-serve-probe:dev
docker build "${REPO_ROOT}/services/status" -t open-serve-status:dev

# --- 2. kind cluster ---------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  log "kind cluster '${CLUSTER_NAME}' already exists — reusing"
else
  log "Creating kind cluster '${CLUSTER_NAME}'"
  kind create cluster --name "${CLUSTER_NAME}" --config "${SCRIPT_DIR}/kind.yaml" --wait 120s
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

# --- 3. Load images into the kind node --------------------------------------
log "Pre-pulling ${RAY_IMAGE} (~1 GB, be patient on first run)"
docker pull "${RAY_IMAGE}"
log "Loading images into the kind node"
kind load docker-image open-serve-gateway:dev --name "${CLUSTER_NAME}"
kind load docker-image "${RAY_IMAGE}" --name "${CLUSTER_NAME}"

# --- 4. KubeRay operator -----------------------------------------------------
log "Installing KubeRay operator ${KUBERAY_CHART_VERSION}"
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ >/dev/null 2>&1 || true
helm repo update kuberay >/dev/null
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "${KUBERAY_CHART_VERSION}" -n "${NAMESPACE}" --wait --timeout 5m

# --- 5. ServiceAccount + API-key Secret --------------------------------------
# The chart expects the Ray pods' ServiceAccount to pre-exist.
kubectl create serviceaccount open-serve-worker -n "${NAMESPACE}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Throwaway local credential, generated at runtime (never committed). On
# re-runs the existing key is reused so a running gateway stays valid.
if kubectl get secret open-serve-api-keys -n "${NAMESPACE}" >/dev/null 2>&1; then
  log "Reusing existing API key from secret open-serve-api-keys"
  API_KEY="$(kubectl get secret open-serve-api-keys -n "${NAMESPACE}" \
    -o jsonpath='{.data.key-map\.json}' | base64 -d \
    | sed -E 's/.*"(sk-test-[0-9a-f]+)".*/\1/')"
else
  API_KEY="sk-test-$(openssl rand -hex 16)"
  log "Creating API-key secret open-serve-api-keys"
  kubectl create secret generic open-serve-api-keys -n "${NAMESPACE}" \
    --from-literal="key-map.json={\"${API_KEY}\": \"test\"}"
fi

# --- 6. open-serve chart -----------------------------------------------------
log "Installing open-serve chart"
# --force-conflicts: on re-runs, Helm 4's server-side apply conflicts with the
# kuberay-operator field manager, which takes ownership of
# RayService.spec.rayClusterConfig.workerGroupSpecs (it writes back the live
# `replicas`). Forcing is safe here: the example pins workerMin=workerMax=1.
helm upgrade --install open-serve "${REPO_ROOT}/charts/open-serve" \
  -n "${NAMESPACE}" -f "${SCRIPT_DIR}/values.yaml" --force-conflicts \
  "${EXTRA_HELM_ARGS[@]+"${EXTRA_HELM_ARGS[@]}"}"

# --- 7. Wait for everything to come up ---------------------------------------
log "Waiting for the gateway deployment"
kubectl rollout status deployment/open-serve-gateway -n "${NAMESPACE}" --timeout 5m

log "Waiting for RayService rayservice-echo to become Ready (up to 15 min)"
kubectl wait rayservice/rayservice-echo -n "${NAMESPACE}" \
  --for=condition=Ready --timeout 15m
kubectl get rayservice,pods -n "${NAMESPACE}"

# --- 8. Assertions through the gateway ---------------------------------------
log "Port-forwarding gateway to localhost:${LOCAL_PORT}"
kubectl port-forward -n "${NAMESPACE}" svc/open-serve-gateway "${LOCAL_PORT}:8000" \
  >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID}" 2>/dev/null || true' EXIT
sleep 3

PASS=0
FAIL=0
check() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "${actual}" == *"${expected}"* ]]; then
    echo "PASS: ${desc}"
    PASS=$((PASS + 1))
  else
    echo "FAIL: ${desc} (expected '${expected}', got '${actual}')"
    FAIL=$((FAIL + 1))
  fi
}

log "Running e2e assertions"
code_unauth="$(curl -s -o /dev/null -w '%{http_code}' "localhost:${LOCAL_PORT}/v1/models")"
check "GET /v1/models without auth returns 401" "401" "${code_unauth}"

models="$(curl -s -H "Authorization: Bearer ${API_KEY}" "localhost:${LOCAL_PORT}/v1/models")"
check "GET /v1/models with auth lists model 'echo'" '"id":"echo"' "${models//[[:space:]]/}"

completion="$(curl -s -H "Authorization: Bearer ${API_KEY}" -H 'Content-Type: application/json' \
  -d '{"model":"echo","messages":[{"role":"user","content":"hello open-serve"}]}' \
  "localhost:${LOCAL_PORT}/v1/chat/completions")"
check "POST /v1/chat/completions echoes the user message" "echo: hello open-serve" "${completion}"

code_health="$(curl -s -o /dev/null -w '%{http_code}' "localhost:${LOCAL_PORT}/healthz")"
check "GET /healthz returns 200 without auth" "200" "${code_health}"

# --- Summary -----------------------------------------------------------------
echo
echo "==============================================="
echo " e2e result: ${PASS} passed, ${FAIL} failed"
echo " API key (local throwaway): ${API_KEY}"
echo " Gateway:  kubectl port-forward -n ${NAMESPACE} svc/open-serve-gateway ${LOCAL_PORT}:8000"
echo " Cleanup:  ${SCRIPT_DIR}/cleanup.sh"
echo "==============================================="
[[ "${FAIL}" -eq 0 ]]
