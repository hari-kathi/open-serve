#!/usr/bin/env bash
# Test all open-serve model endpoints.
#
# Usage: ./test-endpoints.sh BASE_URL [--api-key KEY]
#
#   BASE_URL         gateway base URL (or set BASE_URL env var), e.g.
#                    https://models.example.com or http://127.0.0.1:8000
#   --api-key KEY    bearer token (or set API_KEY env var) — required
#
# Model lists (space-separated env vars):
#   CHAT_MODELS       chat models to exercise            (default: qwen3-8b)
#   EMBED_MODELS      embedding models for /v1/embeddings (default: none)
#   VL_MODELS         vision-language models              (default: none)
#   RESPONSES_MODELS  vllm models for /v1/responses   (default: CHAT_MODELS)
#   TOKENIZE_MODELS   models for /tokenize + /detokenize  (default: CHAT_MODELS)

set -euo pipefail

BASE_URL="${BASE_URL:-}"
API_KEY="${API_KEY:-}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --api-key) API_KEY="$2"; shift 2 ;;
    --help|-h) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *)  BASE_URL="$1"; shift ;;
  esac
done

if [[ -z "$BASE_URL" ]]; then
  echo "error: BASE_URL is required (first argument or BASE_URL env var)" >&2
  echo "usage: $0 BASE_URL [--api-key KEY]" >&2
  exit 2
fi
if [[ -z "$API_KEY" ]]; then
  echo "error: API key is required (--api-key KEY or API_KEY env var)" >&2
  echo "usage: $0 BASE_URL [--api-key KEY]" >&2
  exit 2
fi

CHAT_MODELS="${CHAT_MODELS:-qwen3-8b}"
EMBED_MODELS="${EMBED_MODELS:-}"
VL_MODELS="${VL_MODELS:-}"
RESPONSES_MODELS="${RESPONSES_MODELS:-${CHAT_MODELS}}"
TOKENIZE_MODELS="${TOKENIZE_MODELS:-${CHAT_MODELS}}"
FIRST_CHAT_MODEL=$(echo ${CHAT_MODELS} | awk '{print $1}')

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0

auth_header=""
if [[ -n "$API_KEY" ]]; then
  auth_header="-H \"Authorization: Bearer $API_KEY\""
fi

display_auth() {
  if [[ -n "$auth_header" ]]; then
    echo '-H "Authorization: Bearer ***"'
  fi
}

run_test() {
  local name="$1"
  local cmd="$2"
  local expect_status="${3:-200}"

  # Show request
  local display_cmd="curl -sk $(display_auth) $cmd"
  echo -e "  ${DIM}${display_cmd}${NC}"

  # Execute
  local full_cmd="curl -sk --max-time 30 -w '\n%{http_code}' $auth_header $cmd"
  local output
  output=$(eval "$full_cmd" 2>&1) || true

  local status_code=$(echo "$output" | tail -1)
  local body=$(echo "$output" | sed '$d')

  if [[ "$status_code" == "$expect_status" ]]; then
    echo -e "  ${GREEN}PASS${NC} ${name} ${DIM}(HTTP ${status_code})${NC}"
    PASS=$((PASS + 1))
    if [[ -n "$body" ]]; then
      echo "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if 'data' in d and isinstance(d['data'], list) and len(d['data']) > 0 and 'id' in d['data'][0]:
        ids = [m['id'] for m in d['data']]
        print(f'  \033[2m-> {ids}\033[0m')
    elif 'data' in d and isinstance(d['data'], list) and len(d['data']) > 0 and 'embedding' in d['data'][0]:
        dims = [len(e['embedding']) for e in d['data']]
        tokens = d.get('usage', {}).get('prompt_tokens', '?')
        print(f'  \033[2m-> {len(dims)} embedding(s), dim={dims[0]}, tokens={tokens}\033[0m')
    elif 'choices' in d:
        msg = d['choices'][0]['message']
        tc = msg.get('tool_calls')
        c = msg.get('content')
        if tc:
            calls = [f\"{t['function']['name']}({t['function']['arguments'][:60]})\" for t in tc]
            print(f'  \033[2m-> tool_calls: {calls}\033[0m')
        elif c:
            print(f'  \033[2m-> {c[:120]}\033[0m')
    elif 'tokens' in d:
        print(f'  \033[2m-> count={d[\"count\"]}, max_model_len={d.get(\"max_model_len\",\"?\")}\033[0m')
    elif 'detail' in d:
        print(f'  \033[2m-> {d[\"detail\"]}\033[0m')
    elif 'error' in d:
        e = d['error']
        if isinstance(e, dict):
            print(f'  \033[2m-> {e.get(\"message\", e)}\033[0m')
        else:
            print(f'  \033[2m-> {e}\033[0m')
except:
    pass
" 2>/dev/null || true
    fi
    return 0
  else
    echo -e "  ${RED}FAIL${NC} ${name} ${DIM}(HTTP ${status_code}, expected ${expect_status})${NC}"
    FAIL=$((FAIL + 1))
    if [[ -n "$body" ]]; then
      echo -e "  ${DIM}-> $(echo "$body" | head -1 | cut -c1-120)${NC}"
    fi
    return 1
  fi
}

run_streaming_test() {
  local name="$1"
  local model="$2"
  local prompt="${3:-Who are you?}"
  local max_tokens="${4:-100}"

  local body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"$prompt\"}],\"max_tokens\":$max_tokens,\"stream\":true}"

  # Show request
  echo -e "  ${DIM}curl -sk -N $(display_auth) -H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '${body}'${NC}"

  local full_cmd="curl -sk --max-time 30 -N $auth_header \
    -H 'Content-Type: application/json' \
    '${BASE_URL}/v1/chat/completions' \
    -d '$body'"

  local output
  output=$(eval "$full_cmd" 2>&1) || true

  local chunk_count=$(echo "$output" | grep -c "^data: {" || true)
  local has_done=$(echo "$output" | grep -c "^data: \[DONE\]" || true)
  local has_error=$(echo "$output" | grep -c '"error"' || true)
  local has_finish=$(echo "$output" | grep -c '"finish_reason":"stop\|"finish_reason":"length' || true)

  local content
  content=$(echo "$output" | grep "^data: {" | python3 -c "
import sys, json
tokens = []
for line in sys.stdin:
    line = line.strip()
    if line.startswith('data: {'):
        try:
            d = json.loads(line[6:])
            c = d.get('choices',[{}])[0].get('delta',{}).get('content','')
            if c: tokens.append(c)
        except: pass
print(''.join(tokens)[:120])
" 2>/dev/null || echo "")

  if [[ $chunk_count -gt 0 && $has_finish -gt 0 && $has_error -eq 0 ]]; then
    echo -e "  ${GREEN}PASS${NC} ${name} ${DIM}(${chunk_count} chunks)${NC}"
    echo -e "  ${DIM}-> ${content}${NC}"
    PASS=$((PASS + 1))
    return 0
  else
    echo -e "  ${RED}FAIL${NC} ${name} ${DIM}(chunks=${chunk_count}, done=${has_done}, errors=${has_error})${NC}"
    if [[ -n "$content" ]]; then
      echo -e "  ${DIM}-> ${content}${NC}"
    fi
    echo "$output" | tail -3 | sed "s/^/  /"
    FAIL=$((FAIL + 1))
    return 1
  fi
}

echo ""
echo -e "${BOLD}open-serve endpoint tests${NC}"
echo -e "${DIM}URL:  ${BASE_URL}${NC}"
echo -e "${DIM}Auth: Bearer ***${API_KEY: -6}${NC}"
echo -e "${DIM}Time: $(date -u +%Y-%m-%dT%H:%M:%SZ)${NC}"
echo ""

# --- /v1/models ---
echo -e "${BOLD}GET /v1/models${NC}"
run_test "List models" "'${BASE_URL}/v1/models'" || true
echo ""

# --- /v1/chat/completions (non-streaming) ---
echo -e "${BOLD}POST /v1/chat/completions${NC}"
for model in ${CHAT_MODELS} ${VL_MODELS}; do
  run_test "$model" \
    "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Who are you?\"}],\"max_tokens\":100}'" || true
done
echo ""

# --- /v1/chat/completions (streaming) ---
echo -e "${BOLD}POST /v1/chat/completions (stream)${NC}"
for model in ${CHAT_MODELS} ${VL_MODELS}; do
  run_streaming_test "$model" "$model" "Who are you?" 100 || true
done
echo ""

# --- /v1/chat/completions (tool calling) ---
echo -e "${BOLD}POST /v1/chat/completions (tool calling)${NC}"
TOOL_PAYLOAD="{\"model\":\"${FIRST_CHAT_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"What is the weather in San Francisco?\"}],\"max_tokens\":200,\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"description\":\"Get weather for a location\",\"parameters\":{\"type\":\"object\",\"properties\":{\"location\":{\"type\":\"string\"}},\"required\":[\"location\"]}}}],\"tool_choice\":\"auto\"}"
run_test "${FIRST_CHAT_MODEL} (tool call)" \
  "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '${TOOL_PAYLOAD}'" || true
echo ""

# --- /v1/chat/completions (vision — VL_MODELS only) ---
# 1x1 red PNG as a data URI; VL models should reply "red".
if [[ -n "${VL_MODELS}" ]]; then
  RED_PIXEL='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=='
  echo -e "${BOLD}POST /v1/chat/completions${NC} ${DIM}(vision)${NC}"
  for model in ${VL_MODELS}; do
    VL_PAYLOAD="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"What color is this pixel? Reply with one word.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"${RED_PIXEL}\"}}]}],\"max_tokens\":10}"
    run_test "$model (image)" \
      "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '${VL_PAYLOAD}'" || true
  done
  echo ""
fi

# --- /v1/embeddings ---
if [[ -n "${EMBED_MODELS}" ]]; then
  echo -e "${BOLD}POST /v1/embeddings${NC}"
  for model in ${EMBED_MODELS}; do
    run_test "$model (single)" \
      "-H 'Content-Type: application/json' '${BASE_URL}/v1/embeddings' -d '{\"model\":\"$model\",\"input\":[\"What is deep learning?\"]}'" || true

    run_test "$model (batch)" \
      "-H 'Content-Type: application/json' '${BASE_URL}/v1/embeddings' -d '{\"model\":\"$model\",\"input\":[\"hello world\",\"how are you\",\"test embedding\"]}'" || true
  done
  echo ""
fi

# --- /v1/responses (vllm models only) ---
# Exercises the OpenAI Responses API surface that ray.serve.llm's
# build_openai_app does NOT proxy. Only type:vllm models register
# this route. Any chat-tier model deployed via type:vllm belongs in
# RESPONSES_MODELS.
echo -e "${BOLD}POST /v1/responses${NC} ${DIM}(vllm)${NC}"
for model in ${RESPONSES_MODELS}; do
  run_test "$model" \
    "-H 'Content-Type: application/json' '${BASE_URL}/v1/responses' -d '{\"model\":\"$model\",\"input\":\"Reply with only the number: 7 times 8?\",\"max_output_tokens\":10}'" || true
done
echo ""

# --- /tokenize ---
TOKENIZE_TEXT="Who are you? Tell me about yourself."
echo -e "${BOLD}POST /tokenize${NC} ${DIM}(\"${TOKENIZE_TEXT}\")${NC}"
for model in ${TOKENIZE_MODELS}; do
  run_test "$model" \
    "-H 'Content-Type: application/json' '${BASE_URL}/tokenize' -d '{\"model\":\"$model\",\"prompt\":\"$TOKENIZE_TEXT\"}'" || true
done
echo ""

# --- /detokenize + roundtrip ---
echo -e "${BOLD}POST /detokenize${NC} ${DIM}(roundtrip)${NC}"
for model in ${TOKENIZE_MODELS}; do
  TOKENS_JSON=$(curl -sS -H "Authorization: Bearer ${API_KEY}" -H 'Content-Type: application/json' \
    "${BASE_URL}/tokenize" -d "{\"model\":\"$model\",\"prompt\":\"$TOKENIZE_TEXT\"}" \
    | python3 -c 'import sys, json; d = json.load(sys.stdin); print(json.dumps(d["tokens"]) if "tokens" in d else "")')
  if [[ -z "$TOKENS_JSON" ]]; then
    echo -e "  ${DIM}skip ${model} — /tokenize did not return tokens (model not registered for tokenization?)${NC}"
    continue
  fi
  run_test "$model" \
    "-H 'Content-Type: application/json' '${BASE_URL}/detokenize' -d '{\"model\":\"$model\",\"tokens\":$TOKENS_JSON}'" || true
done
echo ""

# --- Auth enforcement ---
echo -e "${BOLD}Auth enforcement${NC}"
saved_auth="$auth_header"
auth_header=""

run_test "No auth" \
  "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '{\"model\":\"${FIRST_CHAT_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"max_tokens\":5}'" \
  "401" || true

run_test "Bad token" \
  "-H 'Content-Type: application/json' -H 'Authorization: Bearer bad-key' '${BASE_URL}/v1/chat/completions' -d '{\"model\":\"${FIRST_CHAT_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"max_tokens\":5}'" \
  "401" || true

auth_header="$saved_auth"
echo ""

# --- Error handling ---
echo -e "${BOLD}Error handling${NC}"
run_test "Non-existent model" \
  "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '{\"model\":\"no-such-model\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"max_tokens\":5}'" \
  "404" || true

run_test "Empty body" \
  "-H 'Content-Type: application/json' '${BASE_URL}/v1/chat/completions' -d '{}'" \
  "400" || true
echo ""

# --- Summary ---
TOTAL=$((PASS + FAIL))
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}All ${TOTAL} tests passed${NC}"
else
  echo -e "${BOLD}${PASS} passed${NC}, ${RED}${BOLD}${FAIL} failed${NC} ${DIM}(${TOTAL} total)${NC}"
fi
echo ""

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
