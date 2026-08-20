# API reference

All model traffic goes through the gateway, which speaks the OpenAI API shape. Which endpoints a given model actually serves depends on its [runner](../concepts/runners.md).

## Authentication

Every request except the [public paths](#public-paths) requires a Bearer token from the [key map](../operations/api-keys.md):

```
Authorization: Bearer sk-<source>-<hex>
```

- Missing/malformed header → `401 {"detail": "Missing Bearer token"}`
- Unknown key → `401 {"detail": "Invalid API key"}`

Optionally, send `x-openserve-org-id: <org>` (header name configurable) to attribute usage to a downstream org in the metrics.

## Routing

The gateway routes **every** authenticated request by the JSON request body's `model` field — chat, completions, embeddings, `/tokenize`, `/detokenize`, `/v1/responses`, score/rerank, audio alike. The `model` value is looked up in `gateway.modelRoutes` and the request is forwarded to that model's backend. There is no path-based routing and no default backend.

- Body missing the `model` field → `400`:

```json
{"error": {"message": "Request body must include a 'model' field", "type": "invalid_request_error", "code": "missing_model"}}
```

- `model` value not present in `modelRoutes` → `404`:

```json
{"error": {"message": "Unknown model: <model>", "type": "invalid_request_error", "code": "model_not_found"}}
```

## Endpoints by runner

Routing is runner-agnostic — the gateway forwards by the `model` field regardless of runner — but what the backend *answers* depends on the model's [runner](../concepts/runners.md). Requests for an endpoint the runner doesn't serve get a `404` from the backend.

| Endpoint | `vllm` | `ray-serve-llm` | `custom` |
|---|:---:|:---:|:---:|
| `POST /v1/chat/completions` | yes | yes | — |
| `POST /v1/completions` | yes | yes | — |
| `POST /v1/embeddings` | yes | yes | — |
| `GET /v1/models` | yes | yes | — |
| `POST /v1/responses` (OpenAI Responses API) | yes | no | — |
| `POST /tokenize`, `POST /detokenize` | yes | no | — |
| `POST /v1/score`, `POST /v1/rerank` | yes | no | — |
| `POST /pooling`, `POST /classify` | yes | no | — |
| `POST /v1/audio/*` (transcriptions) | yes | no | — |
| Your own routes | — | — | whatever the serve script exposes |

`custom` models expose whatever their serve script defines; requests reach them like any other model — by the `model` field in the body, mapped in `gateway.modelRoutes`.

## `GET /v1/models` aggregation

Each model runs behind its own Service, so any single backend's `/v1/models` only lists its own models. The gateway therefore **does not forward** `GET /v1/models` — it fans out to every backend in `gateway.modelRoutes` and merges the results into one OpenAI-style listing:

- Best-effort: backends that time out, refuse connections, or return non-200 are silently skipped.
- Short per-backend timeout (`MODELS_AGGREGATE_TIMEOUT_S`, default 3s) — a scale-to-zero backend times out and is **dropped from the listing rather than woken up**. An idle model missing from `/v1/models` can still serve requests addressed to it directly.
- Duplicate model ids are deduped (first response wins); output is sorted by id.

## Streaming

Pass `"stream": true` in the request body. The gateway detects it and switches to pass-through SSE proxying — chunks are forwarded as they arrive (`text/event-stream`, buffering disabled). Non-streaming responses are buffered so the gateway can extract `usage` into token metrics.

If you're fronting the gateway with GKE's external Application Load Balancer, note its default 30s backend timeout cuts long streams — the chart's `externalGateway.gcpBackendPolicy` raises it (default 600s).

Backend failures surface as `504` (timeout) / `502` (unreachable) on non-streaming requests, and as a terminal `data: {"error": ...}` SSE event mid-stream.

## Public paths

Served or forwarded **without** auth:

| Path | Behavior |
|---|---|
| `/`, `/health` | Gateway liveness (also used by LB health checks) |
| `/healthz` | Gateway readiness — `503` if not ready |
| `/metrics` | Gateway's own Prometheus metrics |
| `/status`, `/status.json`, `/static/*` | Forwarded (unauthenticated, metered as `source="public"`) to the status page via the `gateway.publicRoutes` prefix map — auto-wired when `statusPage` is enabled. |

## Examples

Set up once:

```bash
export BASE_URL=http://localhost:8000      # or https://models.example.com
export API_KEY=sk-team1-...
```

**Chat completion:**

```bash
curl $BASE_URL/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "What is Ray Serve?"}],
    "max_tokens": 200
  }'
```

**Streaming chat:**

```bash
curl -N $BASE_URL/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "Count to ten."}],
    "stream": true
  }'
```

**Embeddings:**

```bash
curl $BASE_URL/v1/embeddings \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mixedbread-ai/mxbai-embed-large-v1",
    "input": "model serving on kubernetes"
  }'
```

**Tokenize / detokenize** (served natively by every `vllm` model; routed by the `model` field like everything else):

```bash
curl $BASE_URL/tokenize \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen3-8B", "prompt": "hello world"}'

curl $BASE_URL/detokenize \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen3-8B", "tokens": [14990, 1879]}'
```

**List models:**

```bash
curl -s $BASE_URL/v1/models -H "Authorization: Bearer $API_KEY"
```

The OpenAI SDKs work as-is — point `base_url` at the gateway and pass your open-serve key as `api_key`.

For a scripted pass over the whole surface (including auth-failure and error-handling checks), run `scripts/test-endpoints.sh`.
