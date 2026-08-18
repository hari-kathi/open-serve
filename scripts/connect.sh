#!/bin/bash
# Connect to a Ray Serve model: port-forward observability + serve endpoint, then query interactively.
#
# Usage:
#   ./scripts/connect.sh --api-key <key>                              # current kube context, prompt for model
#   ./scripts/connect.sh --cluster my-cluster --api-key <key>         # gcloud auth to a GKE cluster
#   ./scripts/connect.sh --skip-auth --api-key <key> qwen3-8b         # current kubeconfig, auto-select model
#
# Environment:
#   NAMESPACE     Kubernetes namespace (default: open-serve)
#   CLUSTER       cluster name (same as --cluster)
#   GCP_PROJECT   GCP project id (required with --cluster, unless --skip-auth)
#   GCP_REGION    GCP region     (required with --cluster, unless --skip-auth)
#   GATEWAY_SVC   gateway Service name (default: open-serve-gateway)
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-open-serve}"
GCP_PROJECT="${GCP_PROJECT:-}"
GCP_REGION="${GCP_REGION:-}"
GATEWAY_SVC="${GATEWAY_SVC:-open-serve-gateway}"
LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.logs"
mkdir -p "${LOG_DIR}"

CLUSTER="${CLUSTER:-}"
SKIP_AUTH=false
API_KEY="${API_KEY:-}"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Ports
SERVE_PORT=8000
DASHBOARD_PORT=8265
GRAFANA_PORT=3000
PROMETHEUS_PORT=9090

function cleanup() {
  echo ""
  echo -e "${YELLOW}Stopping port-forwards...${NC}"
  pkill -f "kubectl port-forward.*${NAMESPACE}" 2>/dev/null || true
  echo -e "${GREEN}Done.${NC}"
}

function connect_cluster() {
  if [[ "${SKIP_AUTH}" == "true" || -z "${CLUSTER}" ]]; then
    echo -e "${DIM}Using current kubeconfig context: $(kubectl config current-context 2>/dev/null || echo '?')${NC}"
    return
  fi

  if [[ -z "${GCP_PROJECT}" || -z "${GCP_REGION}" ]]; then
    echo -e "${RED}--cluster requires GCP_PROJECT and GCP_REGION env vars (or pass --skip-auth to use the current kubeconfig).${NC}"
    exit 1
  fi

  echo -e "${BLUE}Connecting to ${BOLD}${CLUSTER}${NC}${BLUE} in ${GCP_PROJECT}...${NC}"
  if ! gcloud container clusters get-credentials "${CLUSTER}" \
    --region "${GCP_REGION}" \
    --project "${GCP_PROJECT}" 2>/dev/null; then
    echo -e "${RED}Failed to get credentials for ${CLUSTER}. Check gcloud auth and project access.${NC}"
    exit 1
  fi
  echo -e "${GREEN}Connected to ${CLUSTER}.${NC}"
  echo ""
}

function select_model() {
  # Get all RayServices with status
  local rs_list
  rs_list=$(kubectl get rayservice -n "${NAMESPACE}" --no-headers \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.serviceStatus' 2>/dev/null || true)

  if [[ -z "${rs_list}" ]]; then
    echo -e "${RED}No RayService resources found in namespace ${NAMESPACE}${NC}"
    exit 1
  fi

  echo -e "${BOLD}Available models:${NC}"
  echo ""
  local i=1
  local models=()
  while read -r line; do
    local rs_name status model_name
    rs_name=$(echo "${line}" | awk '{print $1}')
    status=$(echo "${line}" | awk '{print $2}')
    model_name="${rs_name#rayservice-}"
    models+=("${model_name}")
    local color="${GREEN}"
    [[ "${status}" != "Running" ]] && color="${YELLOW}"
    echo -e "  ${i}) ${BOLD}${model_name}${NC}  ${color}${status:-Initializing}${NC}"
    i=$((i + 1))
  done <<< "${rs_list}"
  echo ""

  if [[ -n "${MODEL_ARG}" ]]; then
    MODEL="${MODEL_ARG}"
    echo -e "Selected model: ${BOLD}${MODEL}${NC}"
  elif [[ ${#models[@]} -eq 1 ]]; then
    MODEL="${models[0]}"
    echo -e "Auto-selected model: ${BOLD}${MODEL}${NC}"
  else
    read -rp "Enter number [1]: " choice
    choice=${choice:-1}
    local idx=$((choice - 1))
    if [[ ${idx} -lt 0 || ${idx} -ge ${#models[@]} ]]; then
      echo -e "${RED}Invalid selection.${NC}"
      exit 1
    fi
    MODEL="${models[${idx}]}"
  fi

  # Set head service for the selected model's Ray dashboard
  RAY_HEAD_SVC="rayservice-${MODEL}-head-svc"

  # Derive the *API* model_id and capability (chat vs embedding) from the
  # serve-script ConfigMap. The Helm key (${MODEL}) is a K8s naming artifact;
  # the API model_id may differ (e.g., an embedding model whose API id is
  # "org/model-name" while the Helm key stays short).
  local cm_data
  cm_data=$(kubectl get cm "serve-${MODEL}-script" -n "${NAMESPACE}" \
    -o jsonpath='{.data}' 2>/dev/null || true)
  if [[ -z "${cm_data}" ]]; then
    echo -e "${YELLOW}Could not read serve-${MODEL}-script ConfigMap; falling back to Helm key as model_id and assuming chat capability.${NC}"
    API_MODEL_ID="${MODEL}"
    CAPABILITY="chat"
    IS_VL="false"
  else
    local meta
    meta=$(python3 -c '
import json, re, sys
data = json.loads(sys.argv[1])
script = next(iter(data.values()), "")
mid = re.search(r"model_id=\"([^\"]+)\"", script)
runner = re.search(r"runner=\"([^\"]+)\"", script)
# Vision-language models declare limit_mm_per_prompt in engine_kwargs.
has_mm = bool(re.search(r"limit_mm_per_prompt\s*=", script))
print(mid.group(1) if mid else "")
print("embedding" if (runner and runner.group(1) == "pooling") else "chat")
print("true" if has_mm else "false")
' "${cm_data}")
    API_MODEL_ID=$(echo "${meta}" | sed -n '1p')
    CAPABILITY=$(echo "${meta}" | sed -n '2p')
    IS_VL=$(echo "${meta}" | sed -n '3p')
    if [[ -z "${IS_VL}" ]]; then
      IS_VL="false"
    fi
  fi

  # For `type: vllm-raw` models, the ConfigMap is a placeholder (the serve
  # module is baked into the image, not the ConfigMap), so the
  # `model_id="..."` regex above doesn't match. Fall back to parsing the
  # RayService's serveConfigV2 for the `served_model_name` / `model`
  # entries under `args:`.
  if [[ -z "${API_MODEL_ID}" ]]; then
    local svc_cfg
    svc_cfg=$(kubectl get rayservice "rayservice-${MODEL}" -n "${NAMESPACE}" \
      -o jsonpath='{.spec.serveConfigV2}' 2>/dev/null || true)
    if [[ -n "${svc_cfg}" ]]; then
      local rs_meta
      rs_meta=$(python3 -c '
import re, sys
cfg = sys.argv[1]
# Prefer served_model_name (what clients pass in `model`); fall back to
# the `model:` key (which for vllm-raw is the local mirror path or HF id).
served = re.search(r"served_model_name:\s*\"?([^\"\n#]+)\"?", cfg)
model = re.search(r"^\s+model:\s*\"?([^\"\n#]+)\"?", cfg, re.M)
mid = served or model
print(mid.group(1).strip() if mid else "")
print("true" if re.search(r"limit_mm_per_prompt:", cfg) else "false")
' "${svc_cfg}")
      local rs_mid
      rs_mid=$(echo "${rs_meta}" | sed -n '1p')
      if [[ -n "${rs_mid}" ]]; then
        API_MODEL_ID="${rs_mid}"
        # vllm-raw is currently always chat; future runners (rerank,
        # embedding) would need explicit detection.
        CAPABILITY="chat"
        IS_VL=$(echo "${rs_meta}" | sed -n '2p')
      fi
    fi
  fi

  # Final fallback to the Helm key — preserves prior behavior for models
  # where both lookups failed.
  if [[ -z "${API_MODEL_ID}" ]]; then
    API_MODEL_ID="${MODEL}"
  fi
}

function start_port_forwards() {
  # Kill any existing port-forwards to avoid "port already in use" errors
  pkill -f "kubectl port-forward.*${NAMESPACE}" 2>/dev/null || true
  sleep 1

  echo -e "${BLUE}Starting port-forwards...${NC}"

  # Model serve endpoint (gateway — routes to all backends)
  kubectl port-forward -n "${NAMESPACE}" "svc/${GATEWAY_SVC}" "${SERVE_PORT}:8000" \
    > "${LOG_DIR}/serve-pf.log" 2>&1 &

  # Ray Dashboard (for the selected model)
  kubectl port-forward -n "${NAMESPACE}" "svc/${RAY_HEAD_SVC}" "${DASHBOARD_PORT}:8265" \
    > "${LOG_DIR}/dashboard-pf.log" 2>&1 &

  # Grafana
  kubectl port-forward -n "${NAMESPACE}" svc/kube-prometheus-stack-grafana "${GRAFANA_PORT}:80" \
    > "${LOG_DIR}/grafana-pf.log" 2>&1 &

  # Prometheus
  kubectl port-forward -n "${NAMESPACE}" svc/kube-prometheus-stack-prometheus "${PROMETHEUS_PORT}:9090" \
    > "${LOG_DIR}/prometheus-pf.log" 2>&1 &

  sleep 3

  # Verify port-forwards started
  local failed=0
  for port in ${SERVE_PORT} ${DASHBOARD_PORT} ${GRAFANA_PORT} ${PROMETHEUS_PORT}; do
    if ! lsof -i ":${port}" > /dev/null 2>&1; then
      echo -e "${RED}  Port-forward on port ${port} failed — check ${LOG_DIR}/*.log${NC}"
      failed=1
    fi
  done
  if [[ ${failed} -eq 1 ]]; then
    echo -e "${YELLOW}  Some port-forwards may have failed. Continuing anyway...${NC}"
  fi
}

function print_connection_header() {
  local model_id="$1"
  local capability="$2"
  local is_vl="${3:-false}"

  echo ""
  local cap_label="${capability}"
  [[ "${is_vl}" == "true" ]] && cap_label="${capability} + vision"
  echo -e "${GREEN}${BOLD}Connected to model: ${model_id}${NC} ${DIM}(${cap_label})${NC}"
  echo -e "──────────────────────────────────────────────────────"
  if [[ "${capability}" == "chat" ]]; then
    echo -e "  Chat API:       ${BOLD}http://127.0.0.1:${SERVE_PORT}/v1/chat/completions${NC}"
  else
    echo -e "  Embeddings API: ${BOLD}http://127.0.0.1:${SERVE_PORT}/v1/embeddings${NC}"
  fi
  echo -e "  Tokenize:       ${BOLD}http://127.0.0.1:${SERVE_PORT}/tokenize${NC}"
  echo -e "  Ray Dashboard:  ${BOLD}http://127.0.0.1:${DASHBOARD_PORT}${NC}  (${model_id})"
  echo -e "  Grafana:        ${BOLD}http://127.0.0.1:${GRAFANA_PORT}${NC}  (admin/prom-operator)"
  echo -e "  Prometheus:     ${BOLD}http://127.0.0.1:${PROMETHEUS_PORT}${NC}"
  echo -e "──────────────────────────────────────────────────────"
  if [[ "${is_vl}" == "true" ]]; then
    echo -e "${DIM}Vision model: prefix a prompt with ${YELLOW}/image <path>${NC}${DIM} to attach an image.${NC}"
    echo -e "${DIM}  Example: ${YELLOW}/image ~/Pictures/cat.jpg describe this${NC}"
  fi
  echo ""
}

function query_chat() {
  local model_id="$1"
  local is_vl="${2:-false}"

  echo -e "${BOLD}Chat with ${model_id}${NC} (type ${YELLOW}quit${NC} to exit, ${YELLOW}Ctrl+C${NC} to stop everything)"
  echo ""

  # Disable set -e for the interactive loop so a transient error (model
  # scaling, network blip, malformed chunk) doesn't kill the whole session.
  set +e

  while true; do
    read -rp "You: " prompt
    [[ "${prompt}" == "quit" || "${prompt}" == "exit" ]] && break
    [[ -z "${prompt}" ]] && continue

    # For VL models, allow "/image <path> <rest of prompt>" to attach an image.
    local image_path=""
    local text_prompt="${prompt}"
    if [[ "${is_vl}" == "true" && "${prompt}" == "/image "* ]]; then
      local rest="${prompt#/image }"
      image_path="${rest%% *}"
      text_prompt="${rest#* }"
      # Expand ~ to HOME
      image_path="${image_path/#\~/$HOME}"
      if [[ ! -f "${image_path}" ]]; then
        echo -e "${RED}Image not found: ${image_path}${NC}"
        continue
      fi
      if [[ "${text_prompt}" == "${rest}" ]]; then
        # No space after path — path was the whole thing; prompt it
        text_prompt="Describe this image."
      fi
      echo -e "${DIM}  attaching ${image_path} (${text_prompt:0:60}...)${NC}"
    fi

    echo -ne "${BLUE}${model_id}: ${NC}"
    local payload
    payload=$(IMAGE_PATH="${image_path}" python3 -c '
import json, os, sys, base64, mimetypes
model, text = sys.argv[1], sys.argv[2]
image_path = os.environ.get("IMAGE_PATH", "")
if image_path:
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    content = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
else:
    content = text
print(json.dumps({
    "model": model,
    "messages": [{"role": "user", "content": content}],
    "max_tokens": 512,
    "temperature": 0.7,
    "stream": True,
}))
' "${model_id}" "${text_prompt}")

    # Stream response — print tokens as they arrive
    curl -sS -N -m 300 -X POST \
      "http://127.0.0.1:${SERVE_PORT}/v1/chat/completions" \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${API_KEY}" \
      -d "${payload}" | python3 -u -c "
import sys, json

got_content = False
usage = None
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('data: '):
        continue
    data_str = line[6:]
    if data_str == '[DONE]':
        break
    try:
        d = json.loads(data_str)
        choice = d.get('choices', [{}])[0]
        delta = choice.get('delta', {})
        content = delta.get('content', '')
        if content:
            print(content, end='', flush=True)
            got_content = True
        if choice.get('finish_reason'):
            usage = d.get('usage')
            if usage:
                pt = usage.get('prompt_tokens', '?')
                ct = usage.get('completion_tokens', '?')
                print(f'\n\033[2m[{pt} prompt + {ct} completion tokens]\033[0m')
            else:
                print()
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

if not got_content:
    print('\033[0;31mNo response (model may be scaling up, try again in ~30s)\033[0m')
elif usage is None:
    print()  # ensure trailing newline
"
    echo ""
  done

  set -e
}

function query_embedding() {
  local model_id="$1"

  echo -e "${BOLD}Embed text with ${model_id}${NC} (type ${YELLOW}quit${NC} to exit, ${YELLOW}Ctrl+C${NC} to stop everything)"
  echo ""

  set +e

  while true; do
    read -rp "Text: " prompt
    [[ "${prompt}" == "quit" || "${prompt}" == "exit" ]] && break
    [[ -z "${prompt}" ]] && continue

    local payload
    payload=$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "input": [sys.argv[2]],
}))
' "${model_id}" "${prompt}")

    # Non-streaming: one request, one response
    local resp
    resp=$(curl -sS -m 60 -X POST \
      "http://127.0.0.1:${SERVE_PORT}/v1/embeddings" \
      -H 'Content-Type: application/json' \
      -H "Authorization: Bearer ${API_KEY}" \
      -d "${payload}")

    python3 -u -c "
import sys, json
try:
    d = json.loads(sys.argv[1])
except json.JSONDecodeError:
    print('\033[0;31mInvalid JSON response:\033[0m', sys.argv[1][:500])
    sys.exit(0)

if 'data' in d and d['data']:
    emb = d['data'][0]['embedding']
    usage = d.get('usage', {})
    pt = usage.get('prompt_tokens', '?')
    tt = usage.get('total_tokens', '?')
    print(f'\033[0;34m{sys.argv[2]}:\033[0m dim={len(emb)}  first 4: {[round(x,4) for x in emb[:4]]}  \033[2m[{pt} tokens]\033[0m')
else:
    # surface API error detail if present
    detail = d.get('detail') or d.get('error') or d
    print('\033[0;31mError:\033[0m', json.dumps(detail)[:500])
" "${resp}" "${model_id}"
    echo ""
  done

  set -e
}

# --- Parse Arguments ---

MODEL_ARG=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --cluster)   CLUSTER="$2"; shift 2 ;;
    --skip-auth) SKIP_AUTH=true; shift ;;
    --api-key)   API_KEY="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--cluster NAME] [--namespace NS] [--skip-auth] --api-key KEY [MODEL]"
      echo ""
      echo "Options:"
      echo "  --cluster    GKE cluster name (or CLUSTER env; needs GCP_PROJECT + GCP_REGION env)"
      echo "  --namespace  Kubernetes namespace (or NAMESPACE env; default: open-serve)"
      echo "  --skip-auth  Skip gcloud cluster auth (use current kubeconfig)"
      echo "  --api-key    API key for model authentication (or API_KEY env; required)"
      echo "  MODEL        Model name to auto-select (e.g., qwen3-8b)"
      exit 0 ;;
    -*) echo "Unknown option: $1"; exit 1 ;;
    *)  MODEL_ARG="$1"; shift ;;
  esac
done

if [[ -z "${API_KEY}" ]]; then
  echo -e "${RED}Error: --api-key (or API_KEY env var) is required.${NC}"
  echo "Usage: $0 [--cluster NAME] [--namespace NS] [--skip-auth] --api-key KEY [MODEL]"
  exit 1
fi

# --- Main ---
trap cleanup EXIT

echo -e "${BOLD}open-serve — Model Connect${NC}"
echo ""

# Connect to cluster (or use current kubeconfig)
connect_cluster

# Select model (also sets RAY_HEAD_SVC for dashboard)
select_model

# Start port-forwards
start_port_forwards

# Print connection header and dispatch to the right handler by capability
print_connection_header "${API_MODEL_ID}" "${CAPABILITY}" "${IS_VL:-false}"
case "${CAPABILITY}" in
  chat)      query_chat "${API_MODEL_ID}" "${IS_VL:-false}" ;;
  embedding) query_embedding "${API_MODEL_ID}" ;;
  *)
    echo -e "${RED}Unknown capability: ${CAPABILITY}${NC}"
    exit 1
    ;;
esac
