#!/bin/bash
# Load test Ray Serve models to measure autoscaling and node provisioning time.
#
# Usage:
#   ./scripts/load-test.sh                           # default model list, 10 concurrent, 5 min
#   ./scripts/load-test.sh --model qwen3-8b          # test one model
#   ./scripts/load-test.sh --concurrency 20          # 20 concurrent requests
#   ./scripts/load-test.sh --duration 600            # run for 10 minutes
#   ./scripts/load-test.sh --url http://<internal-lb-ip>:8000  # use an internal LB directly
#
# Environment:
#   NAMESPACE      Kubernetes namespace           (default: open-serve)
#   MODELS         space-separated chat models    (default: qwen3-8b)
#   EMBED_MODELS   space-separated embed models   (default: none)
#   BASE_URL       endpoint base URL              (default: http://127.0.0.1:8000)
#
# Note: this script drives /v1/chat/completions and /v1/embeddings only —
# it does NOT exercise the /v1/responses route. Use scripts/test-endpoints.sh
# for that.
#
set -euo pipefail

# Defaults
NAMESPACE="${NAMESPACE:-open-serve}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
CONCURRENCY=10
DURATION=300
MODELS="${MODELS:-qwen3-8b}"
EMBED_MODELS="${EMBED_MODELS:-}"
MAX_TOKENS=512
PROMPT="Write a detailed essay about the history of artificial intelligence, covering the key milestones from the 1950s to the present day. Include the contributions of Alan Turing, John McCarthy, and Geoffrey Hinton."
EMBED_TEXTS='["What is deep learning?","How does gradient descent work?","Explain backpropagation","Neural network architectures","Transformer attention mechanism"]'

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --model) MODELS="$2"; shift 2 ;;
    --embed-model) EMBED_MODELS="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --url) BASE_URL="$2"; shift 2 ;;
    --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--model MODEL] [--embed-model MODEL] [--concurrency N] [--duration SECS] [--url URL] [--max-tokens N]"
      echo ""
      echo "  --model MODEL        LLM model(s) for chat completions (space-separated)"
      echo "  --embed-model MODEL  Embedding model(s) for /v1/embeddings (space-separated)"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.logs"
mkdir -p "${LOG_DIR}"
RESULTS_FILE="${LOG_DIR}/load-test-$(date +%Y%m%d-%H%M%S).log"
LATENCY_FILE="${LOG_DIR}/load-test-latencies.csv"

# --- Functions ---

function log() {
  local msg="[$(date '+%H:%M:%S')] $1"
  echo -e "${msg}"
  echo -e "${msg}" | sed 's/\x1b\[[0-9;]*m//g' >> "${RESULTS_FILE}"
}

function get_worker_pod_status() {
  kubectl get pods -n "${NAMESPACE}" -l ray.io/node-type=worker \
    -o custom-columns='NAME:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName,MODEL:.metadata.labels.model,AGE:.metadata.creationTimestamp' \
    --no-headers 2>/dev/null
}

function get_gpu_nodes() {
  kubectl get nodes -l workload-type -o custom-columns='NAME:.metadata.name,TYPE:.metadata.labels.workload-type,STATUS:.status.conditions[-1].type,AGE:.metadata.creationTimestamp' \
    --no-headers 2>/dev/null
}

function get_replica_counts() {
  kubectl get rayservices -n "${NAMESPACE}" -o jsonpath='{.items[0].status.numServeEndpoints}' 2>/dev/null || echo "?"
}

# Embedding request sender — runs in background
function embed_request_loop() {
  local model="$1"
  local slots="$2"
  local end_time="$3"

  local payload="{\"model\":\"${model}\",\"input\":${EMBED_TEXTS}}"

  while [[ $(date +%s) -lt ${end_time} ]]; do
    local pids=()
    for _ in $(seq 1 "${slots}"); do
      (
        local t0=$(python3 -c 'import time; print(int(time.time()*1000))')
        local status="ok"
        curl -sf -m 60 -X POST "${BASE_URL}/v1/embeddings" \
          -H 'Content-Type: application/json' \
          -d "${payload}" > /dev/null 2>&1 || status="error"
        local t1=$(python3 -c 'import time; print(int(time.time()*1000))')
        local latency=$((t1 - t0))
        echo "${model},${latency},${status}" >> "${LATENCY_FILE}"
      ) &
      pids+=($!)
    done
    for pid in "${pids[@]}"; do
      wait "${pid}" 2>/dev/null || true
    done
  done
}

# Continuous request sender — runs in background, maintains N concurrent requests
function request_loop() {
  local model="$1"
  local slots="$2"
  local end_time="$3"

  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({
    'model': sys.argv[1],
    'messages': [{'role': 'user', 'content': sys.argv[2]}],
    'max_tokens': int(sys.argv[3]),
    'temperature': 0.7
}))
" "${model}" "${PROMPT}" "${MAX_TOKENS}")

  local req_count=0
  local ok_count=0
  local err_count=0

  while [[ $(date +%s) -lt ${end_time} ]]; do
    # Launch a batch of concurrent requests
    local pids=()
    for _ in $(seq 1 "${slots}"); do
      (
        local t0=$(python3 -c 'import time; print(int(time.time()*1000))')
        local status="ok"
        curl -sf -m 300 -X POST "${BASE_URL}/v1/chat/completions" \
          -H 'Content-Type: application/json' \
          -d "${payload}" > /dev/null 2>&1 || status="error"
        local t1=$(python3 -c 'import time; print(int(time.time()*1000))')
        local latency=$((t1 - t0))
        echo "${model},${latency},${status}" >> "${LATENCY_FILE}"
      ) &
      pids+=($!)
    done

    # Wait for this batch
    for pid in "${pids[@]}"; do
      wait "${pid}" 2>/dev/null || true
    done

    req_count=$((req_count + slots))
  done
}

# Monitor scaling events
function monitor_scaling() {
  local start_time=$1
  local end_time=$2
  local prev_state=""

  while [[ $(date +%s) -lt ${end_time} ]]; do
    local elapsed=$(( $(date +%s) - start_time ))
    local workers
    workers=$(get_worker_pod_status 2>/dev/null)
    local worker_count
    worker_count=$(echo "${workers}" | grep -c . 2>/dev/null || echo 0)
    local node_count
    node_count=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
    local replicas
    replicas=$(get_replica_counts)

    local current_state="${worker_count}|${node_count}|${replicas}"

    if [[ "${current_state}" != "${prev_state}" ]]; then
      log "${YELLOW}[+${elapsed}s] Scaling event:${NC} workers=${worker_count} nodes=${node_count} endpoints=${replicas}"

      if [[ -n "${workers}" ]]; then
        echo "${workers}" | while IFS= read -r line; do
          local pod_status
          pod_status=$(echo "${line}" | awk '{print $2}')
          local color="${GREEN}"
          [[ "${pod_status}" != "Running" ]] && color="${YELLOW}"
          log "  ${color}${line}${NC}"
        done
      fi

      prev_state="${current_state}"
    fi

    sleep 5
  done
}

function print_latency_stats() {
  local model="$1"
  local data
  data=$(grep "^${model}," "${LATENCY_FILE}" 2>/dev/null || true)

  if [[ -z "${data}" ]]; then
    log "${RED}${model}: no data${NC}"
    return
  fi

  local total ok_count err_count
  total=$(echo "${data}" | wc -l | tr -d ' ')
  ok_count=$(echo "${data}" | grep ",ok$" | wc -l | tr -d ' ')
  err_count=$(echo "${data}" | grep ",error$" | wc -l | tr -d ' ')

  if [[ ${ok_count} -gt 0 ]]; then
    local stats
    stats=$(echo "${data}" | grep ",ok$" | awk -F',' '{print $2}' | sort -n | python3 -c "
import sys
vals = [int(x) for x in sys.stdin.read().strip().split('\n') if x]
n = len(vals)
print(f'min={vals[0]}ms p50={vals[n//2]}ms p95={vals[int(n*0.95)]}ms p99={vals[int(n*0.99)]}ms max={vals[-1]}ms')
" 2>/dev/null)

    log "${BOLD}${model}:${NC}"
    log "  Requests:  ${total} total, ${ok_count} ok, ${err_count} errors"
    log "  Latency:   ${stats}"
  else
    log "${RED}${model}: ${total} requests, all errors${NC}"
  fi
}

# --- Cleanup ---
function cleanup() {
  echo ""
  log "${YELLOW}Stopping load generators...${NC}"
  jobs -p | xargs -r kill 2>/dev/null || true
  wait 2>/dev/null || true
  log "${GREEN}Done.${NC}"
}
trap cleanup EXIT

# --- Main ---

echo -e "${BOLD}open-serve — Load Test${NC}"
echo -e "──────────────────────────────────────────────────────"
echo -e "  LLM Models:   ${BOLD}${MODELS:-none}${NC}"
echo -e "  Embed Models: ${BOLD}${EMBED_MODELS:-none}${NC}"
echo -e "  Concurrency:  ${BOLD}${CONCURRENCY}${NC} (per model)"
echo -e "  Duration:     ${BOLD}${DURATION}s${NC}"
echo -e "  Max tokens:   ${BOLD}${MAX_TOKENS}${NC}"
echo -e "  Endpoint:     ${BOLD}${BASE_URL}${NC}"
echo -e "  Results:      ${BOLD}${RESULTS_FILE}${NC}"
echo -e "──────────────────────────────────────────────────────"
echo ""

# Verify endpoint
if ! curl -sf --max-time 5 "${BASE_URL}/v1/models" > /dev/null 2>&1; then
  echo -e "${RED}Cannot reach ${BASE_URL}/v1/models${NC}"
  echo -e "${YELLOW}Start port-forward: kubectl port-forward -n ${NAMESPACE} svc/open-serve-gateway 8000:8000${NC}"
  exit 1
fi

# Init latency file
echo "model,latency_ms,status" > "${LATENCY_FILE}"

# Record initial state
log "${BOLD}=== Initial State ===${NC}"
log ""
log "Worker pods:"
get_worker_pod_status | while IFS= read -r line; do log "  ${line}"; done
log ""
log "GPU nodes:"
get_gpu_nodes | while IFS= read -r line; do log "  ${line}"; done
log ""

START_TIME=$(date +%s)
END_TIME=$((START_TIME + DURATION))

log "Start time: $(date)"
log ""

# Start monitor
log "${BOLD}=== Starting Load Test ===${NC}"
log ""
log "Sending ${CONCURRENCY} sustained concurrent requests per model..."
log "Monitor will report scaling events every 5s."
log ""

monitor_scaling "${START_TIME}" "${END_TIME}" &

# Start continuous request loops (one per model)
for model in ${MODELS}; do
  request_loop "${model}" "${CONCURRENCY}" "${END_TIME}" &
  log "Started LLM load generator for ${BOLD}${model}${NC} (${CONCURRENCY} concurrent)"
done

for model in ${EMBED_MODELS}; do
  embed_request_loop "${model}" "${CONCURRENCY}" "${END_TIME}" &
  log "Started embedding load generator for ${BOLD}${model}${NC} (${CONCURRENCY} concurrent)"
done

# Wait for duration
while [[ $(date +%s) -lt ${END_TIME} ]]; do
  remaining=$((END_TIME - $(date +%s)))
  sleep 10
done

# Let background jobs finish
log ""
log "${YELLOW}Duration complete. Waiting for in-flight requests...${NC}"
wait 2>/dev/null || true

# Record final state
log ""
log "${BOLD}=== Final State ===${NC}"
log ""
log "Worker pods:"
get_worker_pod_status | while IFS= read -r line; do log "  ${line}"; done
log ""
log "GPU nodes:"
get_gpu_nodes | while IFS= read -r line; do log "  ${line}"; done
log ""

TOTAL_TIME=$(($(date +%s) - START_TIME))

log "${BOLD}=== Latency Summary ===${NC}"
log ""
for model in ${MODELS} ${EMBED_MODELS}; do
  print_latency_stats "${model}"
  log ""
done

log "${BOLD}=== Scaling Timeline ===${NC}"
log ""
grep "Scaling event" "${RESULTS_FILE}" 2>/dev/null | while IFS= read -r line; do
  echo -e "${line}"
done
log ""
log "Total duration: ${TOTAL_TIME}s"
log "Full results:   ${RESULTS_FILE}"
log "Latency data:   ${LATENCY_FILE}"

trap - EXIT
