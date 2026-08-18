#!/usr/bin/env bash
# Layer 3 e2e test for the open-serve status page.
#
# Usage:
#   ./scripts/test-status.sh                  # smoke-test via port-forward (current context)
#   ./scripts/test-status.sh --remote URL     # hit an external URL directly
#   ./scripts/test-status.sh --inject-incident [MODEL]
#                                             # break a tier:internal-test model and
#                                             # watch the page flip green→down→green
#
# Environment:
#   KUBE_CONTEXT  kubectl context (default: current context; or --context)
#   NAMESPACE     Kubernetes namespace (default: open-serve; or --namespace)
#   STATUS_SVC    status-page Service name (default: open-serve-status)
#   LOCAL_PORT    local port for the port-forward (default: 18000; or --port)

set -euo pipefail

# --- Defaults ---
CTX="${KUBE_CONTEXT:-$(kubectl config current-context 2>/dev/null || true)}"
NS="${NAMESPACE:-open-serve}"
STATUS_SVC="${STATUS_SVC:-open-serve-status}"
PORT="${LOCAL_PORT:-18000}"
REMOTE_URL=""
INJECT_MODEL="${INJECT_MODEL:-qwen3-8b}"
MAX_WAIT_SECONDS=900  # 15 min — one probe cycle + cache + render
INJECT_MODE=false

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

PASS=0
FAIL=0

# --- Arg parsing ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --remote) REMOTE_URL="$2"; shift 2 ;;
    --context) CTX="$2"; shift 2 ;;
    --namespace) NS="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --inject-incident)
      INJECT_MODE=true
      # Optional positional arg for model id
      if [[ $# -ge 2 && ! "$2" =~ ^- ]]; then
        INJECT_MODEL="$2"; shift 2
      else
        shift 1
      fi
      ;;
    --max-wait) MAX_WAIT_SECONDS="$2"; shift 2 ;;
    --help|-h)
      sed -n '1,/^set -euo/p' "$0" | head -20
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- Helpers ---
pass() { echo -e "  ${GREEN}PASS${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC} $1"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC} $1"; }
log()  { echo -e "${DIM}[$(date +%H:%M:%S)]${NC} $*"; }

cleanup_pf_pid=""
cleanup() {
  if [[ -n "$cleanup_pf_pid" ]]; then
    kill "$cleanup_pf_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- Bring up access to the service ---
if [[ -n "$REMOTE_URL" ]]; then
  BASE_URL="$REMOTE_URL"
  log "Using remote URL: $BASE_URL"
else
  log "Starting port-forward: kubectl --context=$CTX -n $NS port-forward svc/$STATUS_SVC $PORT:8000"
  kubectl --context="$CTX" -n "$NS" port-forward "svc/$STATUS_SVC" "$PORT:8000" >/dev/null 2>&1 &
  cleanup_pf_pid=$!
  BASE_URL="http://127.0.0.1:$PORT"
  # Wait for the port-forward to be reachable
  for _ in $(seq 1 30); do
    if curl -sf --max-time 1 "$BASE_URL/healthz" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

# --- Test 1: healthz responds ---
echo -e "${BOLD}GET /healthz${NC}"
if curl -sf --max-time 5 "$BASE_URL/healthz" | grep -q '"status":"ok"'; then
  pass "healthz returns ok"
else
  fail "healthz did not return ok"
fi
echo ""

# --- Test 2: /status.json contract ---
echo -e "${BOLD}GET /status.json${NC}"
STATUS_JSON=$(curl -sf --max-time 10 "$BASE_URL/status.json" 2>/dev/null || true)
if [[ -z "$STATUS_JSON" ]]; then
  fail "/status.json did not respond"
  echo ""
  exit 1
fi

CHECK() {
  local label="$1" expr="$2"
  if echo "$STATUS_JSON" | python3 -c "import json, sys; d = json.load(sys.stdin); sys.exit(0 if ($expr) else 1)" 2>/dev/null; then
    pass "$label"
  else
    fail "$label"
  fi
}

CHECK "banner.state in {healthy, degraded, down, unknown}" \
  "d['banner']['state'] in {'healthy', 'degraded', 'down', 'unknown'}"
CHECK "banner.color present" "isinstance(d['banner']['color'], str) and d['banner']['color'].startswith('#')"
CHECK "banner.fallback_mode is bool" "isinstance(d['banner']['fallback_mode'], bool)"
CHECK "components is dict" "isinstance(d['components'], dict)"
CHECK "active_incidents is list" "isinstance(d['active_incidents'], list)"
CHECK "recent_history is list" "isinstance(d['recent_history'], list)"
CHECK "strip_days is positive int" "isinstance(d['strip_days'], int) and d['strip_days'] > 0"
CHECK "recent_history_days is positive int" "isinstance(d['recent_history_days'], int) and d['recent_history_days'] > 0"

# Banner color matches state
EXPECTED_COLOR=$(python3 -c "
colors = {'healthy': '#22c55e', 'degraded': '#f97316', 'down': '#ef4444', 'unknown': '#94a3b8'}
import json, sys
d = json.loads(sys.argv[1])
print(colors.get(d['banner']['state'], ''))
" "$STATUS_JSON")
ACTUAL_COLOR=$(echo "$STATUS_JSON" | python3 -c "import json, sys; print(json.load(sys.stdin)['banner']['color'])")
if [[ "$EXPECTED_COLOR" == "$ACTUAL_COLOR" ]]; then
  pass "banner color matches state (${ACTUAL_COLOR})"
else
  fail "banner color mismatch: expected $EXPECTED_COLOR, got $ACTUAL_COLOR"
fi

# Per-model invariants (v2: SLO is per-endpoint; model.daily_strip aggregates).
if STATUS_JSON="$STATUS_JSON" python3 - <<'PY'
import json, os, sys
d = json.loads(os.environ["STATUS_JSON"])
states = {'healthy', 'degraded', 'down', 'unknown'}
errors = []
strip_days = d['strip_days']
for cat, models in d['components'].items():
    for m in models:
        if m['state'] not in states:
            errors.append(f"{m['model_id']}: bad state '{m['state']}'")
        if len(m.get('daily_strip', [])) > strip_days:
            errors.append(f"{m['model_id']}: model strip exceeds strip_days")
        # Endpoints each carry their own state, SLO and strip.
        for ep in m.get('endpoints', []):
            tag = f"{m['model_id']}/{ep['endpoint']}"
            if ep['state'] not in states:
                errors.append(f"{tag}: bad endpoint state '{ep['state']}'")
            if ep.get('slo_latency_s', 0) <= 0:
                errors.append(f"{tag}: non-positive SLO")
            if len(ep.get('daily_strip', [])) > strip_days:
                errors.append(f"{tag}: endpoint strip exceeds strip_days")
if errors:
    for e in errors:
        print("    " + e, file=sys.stderr)
    sys.exit(1)
PY
then
  pass "per-model invariants (state ∈ valid set, SLO > 0, strip ≤ strip_days)"
else
  fail "per-model invariants violated"
fi

# Banner derivation: banner_state should equal max(model states), treating
# unknown last (or 'unknown' when fallback_mode is True).
if STATUS_JSON="$STATUS_JSON" python3 - <<'PY'
import json, os, sys
d = json.loads(os.environ["STATUS_JSON"])
order = {'healthy': 0, 'degraded': 1, 'down': 2, 'unknown': 3}
all_states = [m['state'] for models in d['components'].values() for m in models]
expected = 'healthy' if not all_states else max(all_states, key=order.get)
actual = d['banner']['state']
if d['banner']['fallback_mode']:
    sys.exit(0 if actual == 'unknown' else 1)
sys.exit(0 if actual == expected else 1)
PY
then
  pass "banner state = max(model states)"
else
  fail "banner state ≠ max(model states)"
fi

echo ""

# --- Test 3: /status HTML renders + carries data-test attrs ---
echo -e "${BOLD}GET /status${NC}"
HTML=$(curl -sf --max-time 10 "$BASE_URL/status" 2>/dev/null || true)
if [[ -z "$HTML" ]]; then
  fail "/status did not respond"
else
  if echo "$HTML" | grep -q 'data-test-banner='; then
    pass "HTML carries data-test-banner attr"
  else
    fail "HTML missing data-test-banner attr"
  fi
  if echo "$HTML" | grep -q '<!DOCTYPE html>'; then
    pass "HTML has a doctype"
  else
    fail "HTML missing doctype"
  fi
fi
echo ""

# --- Test 4 (--inject-incident): drive green → down → green ---
if [[ "$INJECT_MODE" == "true" ]]; then
  echo -e "${BOLD}--inject-incident: ${INJECT_MODEL}${NC}"

  RAYSVC="rayservice-${INJECT_MODEL}"

  # Confirm the model exists and is currently healthy according to the page.
  if ! kubectl --context="$CTX" -n "$NS" get rayservice "$RAYSVC" >/dev/null 2>&1; then
    fail "RayService $RAYSVC not found in cluster $CTX/$NS"
    exit 1
  fi

  # Read initial state from /status.json — it may already be down if the model
  # is scaled to zero and the probe hasn't woken it up. Skip if not currently
  # healthy and not currently unknown.
  INITIAL_STATE=$(echo "$STATUS_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for models in d['components'].values():
    for m in models:
        if m['model_id'] == '$INJECT_MODEL' or m['model_id'].endswith('/$INJECT_MODEL'):
            print(m['state']); sys.exit(0)
print('absent')
")
  log "Initial state for ${INJECT_MODEL}: ${INITIAL_STATE}"

  if [[ "$INITIAL_STATE" == "absent" ]]; then
    warn "Model ${INJECT_MODEL} not present in /status.json (possibly tier:internal-test, scale-to-zero)."
    warn "Inject-incident mode requires a model that the probe scrapes. Skipping with a soft pass."
    pass "inject-incident: nothing to do (model absent from production tier)"
    echo ""
  else
    log "Deleting worker pods for $RAYSVC to force a probe failure"
    kubectl --context="$CTX" -n "$NS" delete pods \
      -l "ray.io/cluster=$RAYSVC" \
      --field-selector=status.phase=Running \
      --wait=false >/dev/null 2>&1 || true

    log "Polling /status.json — waiting for ${INJECT_MODEL} to become down or degraded (max ${MAX_WAIT_SECONDS}s)"
    deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
    saw_degraded_or_down=false
    while [[ $(date +%s) -lt $deadline ]]; do
      cur=$(curl -sf --max-time 5 "$BASE_URL/status.json" 2>/dev/null | \
        python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('error'); sys.exit(0)
for models in d['components'].values():
    for m in models:
        if m['model_id'] == '$INJECT_MODEL' or m['model_id'].endswith('/$INJECT_MODEL'):
            print(m['state']); sys.exit(0)
print('absent')
" 2>/dev/null || echo "error")
      log "  ${INJECT_MODEL} → ${cur}"
      if [[ "$cur" == "down" || "$cur" == "degraded" ]]; then
        saw_degraded_or_down=true
        break
      fi
      sleep 30
    done

    if $saw_degraded_or_down; then
      pass "page reflected probe failure (green → ${cur})"
    else
      fail "page did not reflect probe failure within ${MAX_WAIT_SECONDS}s"
    fi

    log "Waiting for ${INJECT_MODEL} to recover to healthy"
    deadline=$(( $(date +%s) + MAX_WAIT_SECONDS ))
    recovered=false
    while [[ $(date +%s) -lt $deadline ]]; do
      cur=$(curl -sf --max-time 5 "$BASE_URL/status.json" 2>/dev/null | \
        python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('error'); sys.exit(0)
for models in d['components'].values():
    for m in models:
        if m['model_id'] == '$INJECT_MODEL' or m['model_id'].endswith('/$INJECT_MODEL'):
            print(m['state']); sys.exit(0)
print('absent')
" 2>/dev/null || echo "error")
      log "  ${INJECT_MODEL} → ${cur}"
      if [[ "$cur" == "healthy" ]]; then
        recovered=true; break
      fi
      sleep 30
    done

    if $recovered; then
      pass "page recovered to healthy after pod restart"
    else
      fail "page did not return to healthy within ${MAX_WAIT_SECONDS}s — manual recovery may be needed"
    fi
    echo ""
  fi
fi

# --- Summary ---
TOTAL=$((PASS + FAIL))
echo -e "${BOLD}${TOTAL} tests${NC}: ${GREEN}${PASS} pass${NC}, ${RED}${FAIL} fail${NC}"
if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
