"""open-serve Probe — synthetic probe for tier=production models.

Runs as a long-lived Deployment with an internal scheduler that fires every
INTERVAL_SECONDS. Two probe paths per cycle for each target:

* Internal — direct to each model's RayService (rayservice-<name>-serve-svc:8000),
             bypassing the auth proxy. Verifies the head pod is up and (for
             OpenAI-API runners) the model is registered. No Bearer token
             needed (in-cluster Service).

* External — via the gateway (e.g. https://models.example.com). Functional
             probe (per-runner request shape) exercises the full path:
             cert + GKE Gateway + auth proxy + model routing + worker.
             Bearer auth required.

Internal-test models are never probed — the deployment config filters the
target list to tier=production.

================================================================================
Adding a new runner type (extension contract)
================================================================================

The deployment's per-model `runner` field is open-ended — any string is
allowed. The probe code dispatches on the value:

* `chat`      — built-in. Liveness = GET /v1/models (asserts model_id),
                Functional = POST /v1/chat/completions.
* `embedding` — built-in. Liveness = GET /v1/models (asserts model_id),
                Functional = POST /v1/embeddings.
* anything else (e.g., `rerank`, `classify`, `ocr`, `custom`) —
                Liveness = GET /-/healthy (Ray Serve's universal health
                endpoint, returns 200 if the head pod is up). Functional
                probe is SKIPPED until a tailored handler is added.

To add a tailored functional probe for a new runner type:

1. Define an async `_probe_<type>(client, base_url, target)` function that
   sends the request shape that runner expects and asserts the response
   contains the expected output (e.g., reranker response is a sorted list
   of scores).
2. Add the dispatch line in `_probe_target()`:
     elif runner == "<type>":
         await _probe_<type>(client, EXTERNAL_URL, target)
3. (Optional) If the new runner uses a non-OpenAI liveness path, override
   the liveness behavior in `_probe_target` instead of using the default
   /-/healthy fallback.

That's it. No deployment-config changes required for the runner field itself;
the config just passes the string through to the probe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import yaml
from fastapi import FastAPI, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("openserve-probe")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the probe scheduler on app startup; cancel it on shutdown."""
    logger.info(
        "open-serve probe starting: interval=%ds, targets=%d, external=%s, auth=%s",
        INTERVAL_SECONDS,
        len(TARGETS),
        EXTERNAL_URL or "off",
        "on" if BEARER_TOKEN else "off",
    )
    task = asyncio.create_task(probe_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="open-serve Probe", lifespan=lifespan)

# --- Configuration ---

CONFIG_PATH = os.environ.get("PROBE_CONFIG_FILE", "/config/targets.yaml")
KEY_MAP_FILE = os.environ.get("API_KEY_MAP_FILE", "/secrets/key-map.json")

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

INTERVAL_SECONDS = int(CONFIG.get("schedule", {}).get("intervalSeconds", 900))
DEFAULT_TIMEOUT_SECONDS = int(CONFIG.get("defaultTimeoutSeconds", 30))
EXTERNAL_URL: str | None = CONFIG.get("externalUrl")
TARGETS: list[dict] = CONFIG.get("targets", [])
AUTH_BEARER_KEY: str | None = CONFIG.get("authBearerKey")

# Bearer token used only for external probes (auth proxy). Internal probes go
# direct to the RayService Service and don't need auth.
BEARER_TOKEN: str | None = None
if AUTH_BEARER_KEY and os.path.exists(KEY_MAP_FILE):
    with open(KEY_MAP_FILE) as f:
        key_map: dict[str, str] = json.load(f)
    for token, source in key_map.items():
        if source == AUTH_BEARER_KEY:
            BEARER_TOKEN = token
            break
    if BEARER_TOKEN is None:
        logger.warning("no key found for source %s in key map; external probe will run without auth", AUTH_BEARER_KEY)

PRODUCTION_TIER = "production"
OPENAI_RUNNERS = frozenset({"chat", "embedding"})

# --- Prometheus metrics ---

probe_success = Gauge(
    "openserve_probe_success",
    "Probe outcome (1=success, 0=failure) by model, endpoint, path, tier",
    ["model", "endpoint", "path", "tier"],
)
probe_latency = Histogram(
    "openserve_probe_latency_seconds",
    "End-to-end probe latency",
    ["model", "endpoint", "path", "tier"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 180],
)
probe_last_success = Gauge(
    "openserve_probe_last_success_timestamp",
    "Unix timestamp of the last successful probe",
    ["model", "endpoint", "path", "tier"],
)
probe_errors = Counter(
    "openserve_probe_errors_total",
    "Total probe errors by model, endpoint, path, tier, error type",
    ["model", "endpoint", "path", "tier", "error_type"],
)
probe_runs = Counter(
    "openserve_probe_runs_total",
    "Total probe cycles started",
)


# --- Probe execution ---

def _external_headers() -> dict[str, str]:
    """Headers for external probes (via auth proxy)."""
    h = {"Content-Type": "application/json"}
    if BEARER_TOKEN:
        h["Authorization"] = f"Bearer {BEARER_TOKEN}"
    return h


def _internal_headers() -> dict[str, str]:
    """Headers for internal probes (direct to RayService, no auth)."""
    return {"Content-Type": "application/json"}


def _record_failure(labels: dict[str, str], elapsed: float, error_type: str, msg: str) -> None:
    probe_latency.labels(**labels).observe(elapsed)
    probe_success.labels(**labels).set(0)
    probe_errors.labels(**labels, error_type=error_type).inc()
    logger.warning("probe failure %s: %s", labels, msg)


def _record_success(labels: dict[str, str], elapsed: float) -> None:
    probe_latency.labels(**labels).observe(elapsed)
    probe_success.labels(**labels).set(1)
    probe_last_success.labels(**labels).set(time.time())


async def _probe_liveness_models(client: httpx.AsyncClient, base_url: str, target: dict) -> None:
    """GET /v1/models — OpenAI-API liveness for chat/embedding runners.

    Asserts model_id is in the response. Served by the head pod's
    OpenAiIngress; does NOT wake worker replicas.
    """
    model_id = target["modelId"]
    path = "/v1/models"
    timeout = target.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    labels = {"model": model_id, "endpoint": "internal", "path": path, "tier": PRODUCTION_TIER}

    start = time.time()
    try:
        resp = await client.get(f"{base_url}{path}", headers=_internal_headers(), timeout=timeout)
        elapsed = time.time() - start
        if resp.status_code != 200:
            _record_failure(labels, elapsed, f"status_{resp.status_code}", f"got HTTP {resp.status_code}")
            return
        data = resp.json()
        ids = [m.get("id") for m in data.get("data", [])]
        if model_id not in ids:
            _record_failure(labels, elapsed, "model_not_listed", f"{model_id} not in /v1/models response: {ids}")
            return
        _record_success(labels, elapsed)
    except httpx.TimeoutException:
        _record_failure(labels, time.time() - start, "timeout", "request timed out")
    except (httpx.ConnectError, httpx.NetworkError):
        _record_failure(labels, time.time() - start, "connect_error", "connection failed")
    except json.JSONDecodeError as e:
        _record_failure(labels, time.time() - start, "decode_error", f"{e}")
    except Exception as e:
        _record_failure(labels, time.time() - start, "unexpected", f"{type(e).__name__}: {e}")


async def _probe_liveness_health(client: httpx.AsyncClient, base_url: str, target: dict) -> None:
    """GET /-/healthy — Ray Serve's universal health endpoint, runner-agnostic.

    Used as fallback for non-OpenAI runners (rerankers, classifiers, OCR,
    etc.). Returns 200 if the head pod is up and Ray Serve is healthy.
    Weaker than _probe_liveness_models because it doesn't verify any
    specific model is registered, but it works for any Ray Serve app.
    """
    model_id = target["modelId"]
    path = "/-/healthy"
    timeout = target.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    labels = {"model": model_id, "endpoint": "internal", "path": path, "tier": PRODUCTION_TIER}

    start = time.time()
    try:
        resp = await client.get(f"{base_url}{path}", headers=_internal_headers(), timeout=timeout)
        elapsed = time.time() - start
        if resp.status_code != 200:
            _record_failure(labels, elapsed, f"status_{resp.status_code}", f"got HTTP {resp.status_code}")
            return
        _record_success(labels, elapsed)
    except httpx.TimeoutException:
        _record_failure(labels, time.time() - start, "timeout", "request timed out")
    except (httpx.ConnectError, httpx.NetworkError):
        _record_failure(labels, time.time() - start, "connect_error", "connection failed")
    except Exception as e:
        _record_failure(labels, time.time() - start, "unexpected", f"{type(e).__name__}: {e}")


async def _probe_chat(client: httpx.AsyncClient, base_url: str, target: dict) -> None:
    """POST /v1/chat/completions via gateway. Exercises full
    gateway+auth-proxy+routing+worker path. May wake cold workers (production
    tier has min_replicas>=1 so usually warm).
    """
    model_id = target["modelId"]
    path = "/v1/chat/completions"
    timeout = target.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    labels = {"model": model_id, "endpoint": "external", "path": path, "tier": PRODUCTION_TIER}

    body: dict = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    body.update(target.get("requestBodyOverrides", {}))

    start = time.time()
    try:
        resp = await client.post(f"{base_url}{path}", headers=_external_headers(), json=body, timeout=timeout)
        elapsed = time.time() - start
        if resp.status_code != 200:
            _record_failure(labels, elapsed, f"status_{resp.status_code}", f"got HTTP {resp.status_code}: {resp.text[:200]}")
            return
        data = resp.json()
        if not data.get("choices"):
            _record_failure(labels, elapsed, "empty_response", "no choices in response")
            return
        _record_success(labels, elapsed)
    except httpx.TimeoutException:
        _record_failure(labels, time.time() - start, "timeout", "request timed out")
    except (httpx.ConnectError, httpx.NetworkError):
        _record_failure(labels, time.time() - start, "connect_error", "connection failed")
    except json.JSONDecodeError as e:
        _record_failure(labels, time.time() - start, "decode_error", f"{e}")
    except Exception as e:
        _record_failure(labels, time.time() - start, "unexpected", f"{type(e).__name__}: {e}")


async def _probe_embedding(client: httpx.AsyncClient, base_url: str, target: dict) -> None:
    """POST /v1/embeddings via gateway. Production embedding models only."""
    model_id = target["modelId"]
    path = "/v1/embeddings"
    timeout = target.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    labels = {"model": model_id, "endpoint": "external", "path": path, "tier": PRODUCTION_TIER}

    body: dict = {"model": model_id, "input": "ping"}
    body.update(target.get("requestBodyOverrides", {}))

    start = time.time()
    try:
        resp = await client.post(f"{base_url}{path}", headers=_external_headers(), json=body, timeout=timeout)
        elapsed = time.time() - start
        if resp.status_code != 200:
            _record_failure(labels, elapsed, f"status_{resp.status_code}", f"got HTTP {resp.status_code}: {resp.text[:200]}")
            return
        data = resp.json()
        if not (data.get("data") and data["data"][0].get("embedding")):
            _record_failure(labels, elapsed, "empty_response", "no embedding in response")
            return
        _record_success(labels, elapsed)
    except httpx.TimeoutException:
        _record_failure(labels, time.time() - start, "timeout", "request timed out")
    except (httpx.ConnectError, httpx.NetworkError):
        _record_failure(labels, time.time() - start, "connect_error", "connection failed")
    except json.JSONDecodeError as e:
        _record_failure(labels, time.time() - start, "decode_error", f"{e}")
    except Exception as e:
        _record_failure(labels, time.time() - start, "unexpected", f"{type(e).__name__}: {e}")


async def _probe_target(client: httpx.AsyncClient, target: dict) -> None:
    """Probe a single production target across internal liveness and external functional.

    Dispatch on the runner field — see the module docstring's "Adding a new
    runner type" section for the extension contract.
    """
    internal_url = target.get("internalUrl")
    runner = target.get("runner", "chat")
    is_openai_runner = runner in OPENAI_RUNNERS

    # Internal liveness — runner-aware path.
    if internal_url:
        if is_openai_runner:
            await _probe_liveness_models(client, internal_url, target)
        else:
            await _probe_liveness_health(client, internal_url, target)

    # External functional probe — only built-in runners. New runner types fall
    # through and emit liveness only until a tailored handler is added.
    if EXTERNAL_URL:
        if runner == "chat":
            await _probe_chat(client, EXTERNAL_URL, target)
        elif runner == "embedding":
            await _probe_embedding(client, EXTERNAL_URL, target)
        else:
            logger.info(
                "no functional probe registered for runner=%s (model=%s); add a _probe_<type> handler in main.py",
                runner, target.get("modelId"),
            )


async def probe_once() -> None:
    """One full probe cycle — every production target."""
    probe_runs.inc()
    logger.info("probe cycle starting: %d targets, external=%s", len(TARGETS), bool(EXTERNAL_URL))
    async with httpx.AsyncClient(verify=True) as client:
        await asyncio.gather(*[_probe_target(client, t) for t in TARGETS], return_exceptions=False)
    logger.info("probe cycle complete")


async def probe_loop() -> None:
    """Main scheduler loop."""
    while True:
        try:
            await probe_once()
        except Exception:
            logger.exception("probe cycle errored")
        await asyncio.sleep(INTERVAL_SECONDS)


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type="text/plain")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "interval_seconds": INTERVAL_SECONDS, "targets": len(TARGETS)}


@app.get("/")
async def root() -> dict:
    return {"status": "ok"}
