"""open-serve Gateway — API key authentication, model-based routing, and usage metrics.

Routing contract, in order of precedence:

1. Paths in SKIP_AUTH_PATHS ("/", "/health", "/healthz", "/metrics") are
   handled locally by the gateway itself — no auth, no backend involved.
2. Paths matching a PUBLIC_ROUTES prefix (JSON map of path-prefix → backend
   URL) are forwarded to that backend WITHOUT auth. This is how the public
   status page is exposed through the gateway.
3. Everything else requires a valid Bearer key, then routes purely by model:
   a. GET /v1/models — fan out to the unique set of MODEL_ROUTES backends
      concurrently and merge the listings.
   b. GET/DELETE /v1/models/<id> — route by <id> via MODEL_ROUTES.
   c. Any other request — parse the JSON body's top-level `model` field and
      look it up in MODEL_ROUTES (modelId → backend URL). A missing or
      unparseable model is a 400; a model not in MODEL_ROUTES is a 404.
      vLLM accepts `model` in every request body (chat, completions,
      embeddings, responses, tokenize/detokenize, rerank), so this covers
      every endpoint.
"""

import asyncio
import json
import logging
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram, generate_latest

logger = logging.getLogger("openserve-gateway")

app = FastAPI(title="open-serve Gateway")

# --- Configuration ---

# API key → source mapping (loaded from Secret volume mount or env var)
_key_map_file = os.environ.get("API_KEY_MAP_FILE")
if _key_map_file and os.path.exists(_key_map_file):
    with open(_key_map_file) as f:
        API_KEYS: dict[str, str] = json.load(f)
else:
    API_KEYS = json.loads(os.environ.get("API_KEY_MAP", "{}"))

# Model-based routing: model name → backend URL. The only routing table for
# authenticated traffic — a model absent from this map does not exist.
MODEL_ROUTES: dict[str, str] = json.loads(os.environ.get("MODEL_ROUTES", "{}"))

# Unauthenticated path-prefix forwarding: path prefix → backend URL, e.g.
# {"/status": "http://open-serve-status:8080", "/static/": "http://open-serve-status:8080"}.
# Used for the public status page; empty by default so nothing is exposed
# unless an operator opts in.
PUBLIC_ROUTES: dict[str, str] = json.loads(os.environ.get("PUBLIC_ROUTES", "{}"))

# Model → tier classification (production | internal-test). Loaded from ConfigMap
# volume or env var. Used to label metrics so SLO dashboards and alerts can
# filter customer-facing models from R&D / scale-to-zero models.
_tier_map_file = os.environ.get("MODEL_TIER_MAP_FILE")
if _tier_map_file and os.path.exists(_tier_map_file):
    with open(_tier_map_file) as f:
        MODEL_TIER_MAP: dict[str, str] = json.load(f)
else:
    MODEL_TIER_MAP = json.loads(os.environ.get("MODEL_TIER_MAP", "{}"))

# Per-backend timeout for the /v1/models fan-out. Cold scale-to-zero
# backends will exceed this and be silently dropped from the listing
# rather than waking them up just for a directory call. Override via
# env for environments with slow control planes.
MODELS_AGGREGATE_TIMEOUT_S = float(os.environ.get("MODELS_AGGREGATE_TIMEOUT_S", "3.0"))

# Reusable HTTP clients (one per backend URL, connection pooling)
_clients: dict[str, httpx.AsyncClient] = {}


def _client_for(url: str) -> httpx.AsyncClient:
    """Return the cached httpx client for a backend, creating one on miss."""
    if url not in _clients:
        _clients[url] = httpx.AsyncClient(base_url=url, timeout=300.0)
    return _clients[url]


def _is_streaming_request(body: bytes) -> bool:
    """Check if the request body contains stream=true."""
    try:
        return json.loads(body).get("stream", False) is True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _openai_error(status: int, message: str, param: str | None = None) -> JSONResponse:
    """OpenAI-style error envelope for routing rejections."""
    error: dict = {"message": message, "type": "invalid_request_error"}
    if param is not None:
        error["param"] = param
    return JSONResponse(status_code=status, content={"error": error})


async def aggregate_models() -> JSONResponse:
    """Fan out GET /v1/models across every distinct MODEL_ROUTES backend and
    merge the results into a single OpenAI-compatible listing.

    Each model on open-serve is its own Ray Serve app behind its own
    Service, so any single backend's /v1/models response only lists that
    one app's own models. Without aggregation, callers could never see
    the full catalog.

    Backends that timeout, fail to connect, or respond non-200 are
    skipped — listing is best-effort. A scale-to-zero backend will
    typically time out within MODELS_AGGREGATE_TIMEOUT_S and is dropped
    from this response rather than woken up just for a listing.
    Duplicate model ids (same model fronted by multiple Services) are
    deduped, first response wins. Output is sorted by id for stability.
    """

    async def fetch_one(url: str) -> list[dict]:
        try:
            resp = await _client_for(url).get("/v1/models", timeout=MODELS_AGGREGATE_TIMEOUT_S)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json().get("data") or []
        except (json.JSONDecodeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    results = await asyncio.gather(*(fetch_one(u) for u in set(MODEL_ROUTES.values())))
    merged: dict[str, dict] = {}
    for entries in results:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if isinstance(mid, str) and mid not in merged:
                merged[mid] = entry
    payload = {
        "object": "list",
        "data": sorted(merged.values(), key=lambda m: m.get("id", "")),
    }
    return JSONResponse(content=payload)


# --- Prometheus Metrics ---

request_counter = Counter(
    "openserve_requests_total",
    "Total requests by source, model, org, and tier",
    ["source", "model", "org", "tier"],
)
error_counter = Counter(
    "openserve_errors_total",
    "Total errors by source, model, error type, and tier",
    ["source", "model", "error_type", "tier"],
)
token_counter = Counter(
    "openserve_tokens_total",
    "Total tokens by source, model, org, type, and tier",
    ["source", "model", "org", "token_type", "tier"],
)
latency_histogram = Histogram(
    "openserve_request_duration_seconds",
    "Request latency by source, model, and tier",
    ["source", "model", "tier"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
)

# --- Middleware ---

# Paths handled by FastAPI's own routes (the proxy's `/`, `/health`,
# `/healthz`, `/metrics`). No auth and no forward — they short-circuit
# straight to the in-process handler.
SKIP_AUTH_PATHS = frozenset({"/", "/health", "/healthz", "/metrics"})

# Header carrying the caller's organization id, used for the `org` metric
# label. Configurable so deployments can keep an existing header name.
ORG_ID_HEADER = os.environ.get("ORG_ID_HEADER", "x-openserve-org-id")


def _public_route_for(path: str) -> str | None:
    """Backend URL for an unauthenticated public-prefix path, or None."""
    for prefix, url in PUBLIC_ROUTES.items():
        if path.startswith(prefix):
            return url
    return None


@app.middleware("http")
async def auth_and_track(request: Request, call_next):
    path = request.url.path

    # Locally-served endpoints (proxy's own routes). No auth, no forward.
    if path in SKIP_AUTH_PATHS:
        return await call_next(request)

    # Public-prefix forwards (status page). No auth; source="public" keeps
    # this traffic in a separate metrics bucket from authenticated calls.
    public_url = _public_route_for(path)
    if public_url is not None:
        body = await request.body()
        request_counter.labels(source="public", model="unknown", org="unknown", tier="unknown").inc()
        return await _handle_standard(
            _client_for(public_url), request, _forward_headers(request), body,
            "public", "unknown", "unknown", "unknown",
        )

    # Validate API key
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        error_counter.labels(source="unknown", model="unknown", error_type="auth_failed", tier="unknown").inc()
        return JSONResponse(status_code=401, content={"detail": "Missing Bearer token"})
    key = auth[7:]
    source = API_KEYS.get(key)
    if source is None:
        error_counter.labels(source="unknown", model="unknown", error_type="auth_failed", tier="unknown").inc()
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})

    # Extract the caller's org id from the configurable custom header
    org = request.headers.get(ORG_ID_HEADER, "unknown")

    # GET /v1/models is special: every model on open-serve is its own
    # Ray Serve app, so forwarding to a single backend would always omit
    # every other model. Aggregate across all MODEL_ROUTES backends instead.
    if request.method == "GET" and path == "/v1/models":
        request_counter.labels(source=source, model="<list>", org=org, tier="aggregate").inc()
        return await aggregate_models()

    # GET/DELETE /v1/models/<id> carries the model in the path, not the body.
    if request.method in ("GET", "DELETE") and path.startswith("/v1/models/"):
        model = path[len("/v1/models/"):]
    else:
        # Everything else routes by the JSON body's top-level `model` field.
        body = await request.body()
        try:
            model = json.loads(body).get("model")
        except (json.JSONDecodeError, UnicodeDecodeError):
            model = None
        if not isinstance(model, str) or not model:
            error_counter.labels(source=source, model="unknown", error_type="missing_model", tier="unknown").inc()
            return _openai_error(400, "request body must include a 'model' field")

    tier = MODEL_TIER_MAP.get(model, "unknown")

    backend_url = MODEL_ROUTES.get(model)
    if backend_url is None:
        error_counter.labels(source=source, model=model, error_type="unknown_model", tier=tier).inc()
        return _openai_error(404, f"unknown model '{model}'", param="model")
    client = _client_for(backend_url)

    body = await request.body()
    forward_headers = _forward_headers(request)

    # Record request metrics (before forwarding — latency recorded after)
    request_counter.labels(source=source, model=model, org=org, tier=tier).inc()

    if _is_streaming_request(body):
        return await _handle_streaming(client, request, forward_headers, body, source, model, org, tier)
    else:
        return await _handle_standard(client, request, forward_headers, body, source, model, org, tier)


def _forward_headers(request: Request) -> dict[str, str]:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }


async def _handle_streaming(client, request, forward_headers, body, source, model, org, tier):
    """Handle streaming (SSE) requests — forward chunks as they arrive."""
    start = time.time()

    async def stream_generator():
        try:
            async with client.stream(
                method=request.method,
                url=request.url.path,
                headers=forward_headers,
                content=body,
                params=dict(request.query_params),
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

                elapsed = time.time() - start
                latency_histogram.labels(source=source, model=model, tier=tier).observe(elapsed)

                # Try to extract token usage from the last SSE data line
                # SSE format: "data: {json}\n\n" — the last data line before [DONE] has usage
                # We can't reliably parse mid-stream, so just log latency
                logger.info(
                    "streaming request completed",
                    extra={"source": source, "org": org, "model": model,
                           "latency_s": round(elapsed, 3), "streaming": True},
                )
        except httpx.TimeoutException:
            error_counter.labels(source=source, model=model, error_type="timeout", tier=tier).inc()
            yield b'data: {"error": "Backend timeout"}\n\n'
        except httpx.ConnectError:
            error_counter.labels(source=source, model=model, error_type="backend_error", tier=tier).inc()
            yield b'data: {"error": "Backend unavailable"}\n\n'

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _handle_standard(client, request, forward_headers, body, source, model, org, tier):
    """Handle standard (non-streaming) requests — buffer response, extract metrics."""
    start = time.time()
    try:
        resp = await client.request(
            method=request.method,
            url=request.url.path,
            headers=forward_headers,
            content=body,
            params=dict(request.query_params),
        )
    except httpx.TimeoutException:
        error_counter.labels(source=source, model=model, error_type="timeout", tier=tier).inc()
        return JSONResponse(status_code=504, content={"detail": "Backend timeout"})
    except httpx.ConnectError:
        error_counter.labels(source=source, model=model, error_type="backend_error", tier=tier).inc()
        return JSONResponse(status_code=502, content={"detail": "Backend unavailable"})

    elapsed = time.time() - start
    latency_histogram.labels(source=source, model=model, tier=tier).observe(elapsed)

    # Extract token usage from response
    if resp.status_code == 200:
        try:
            resp_data = resp.json()
            usage = resp_data.get("usage", {})
            if usage:
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                token_counter.labels(
                    source=source, model=model, org=org, token_type="prompt", tier=tier
                ).inc(prompt_tokens)
                token_counter.labels(
                    source=source, model=model, org=org, token_type="completion", tier=tier
                ).inc(completion_tokens)
                logger.info(
                    "request completed",
                    extra={
                        "source": source,
                        "org": org,
                        "model": model,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "latency_s": round(elapsed, 3),
                    },
                )
        except (json.JSONDecodeError, KeyError):
            pass
    else:
        error_counter.labels(source=source, model=model, error_type="backend_error", tier=tier).inc()

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")
        },
        media_type=resp.headers.get("content-type"),
    )


# --- Endpoints ---


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain")


@app.get("/")
async def root():
    """Root endpoint for GKE Gateway health checks."""
    return {"status": "ok"}


@app.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/healthz")
async def healthz():
    """Readiness probe — ready once the API key map is loaded. No backend
    is probed: routing is per-model, so no single backend's health says
    anything about the gateway's ability to serve."""
    if not API_KEYS:
        raise HTTPException(status_code=503, detail="API key map not loaded")
    return {"status": "ok"}
