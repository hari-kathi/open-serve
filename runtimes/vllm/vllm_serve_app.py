"""Plain Ray Serve + vLLM application module — __call__ + ASGI passthrough.

Mounts vLLM's full OpenAI-compatible FastAPI app inside a Ray Serve
deployment using Ray's canonical __call__ pattern (matches the upstream
`doc/source/serve/doc_code/vllm_example.py` example). Per request, the
deployment dispatches the incoming Starlette `Request` into vLLM's app
via the ASGI protocol and streams the response back through Ray Serve's
HTTP proxy via `StreamingResponse`. Every route vLLM registers —
/v1/responses, /v1/score, /v1/rerank, /tokenize, /detokenize, /pooling,
/classify, audio transcriptions, plus the standard chat/completions/
embeddings — is reachable end-to-end.

This module is the chart's `type: vllm` import target. Reference it
from the per-model RayService as:

    serveConfigV2: |
      applications:
      - name: qwen3-8b
        import_path: vllm_serve_app:build_app_fn
        route_prefix: "/"
        args:
          model: Qwen/Qwen3-8B
          model_source: gs://<your-model-bucket>/models/Qwen/Qwen3-8B
          tensor_parallel_size: 1
          dtype: bfloat16
          max_model_len: 32768
          # ... every other vLLM CLI flag forwards via _parse_vllm_args
        deployments:
        - name: VLLMOpenAI
          ray_actor_options: { num_gpus: 1 }
          autoscaling_config:
            min_replicas: 0
            max_replicas: 1
            target_ongoing_requests: 5

Why __call__ + ASGI passthrough (not @serve.ingress)
----------------------------------------------------
The original research write-up used `@serve.ingress(app)` with a module-
level FastAPI `app` and merged vLLM's route table onto it. That pattern
breaks in our split topology — proxy on the head pod, replica on the
worker pod, separate Python processes:

1. Module-level `app` is per-process. Mutating `app.router.routes` in
   the replica doesn't propagate to the proxy → proxy 404s every route.
2. Moving the merge into a build_app_fn that runs per-actor fixes
   visibility but trips two new failures: vLLM's `make_arg_parser`
   triggers `DeviceConfig.__post_init__` (fails on the no-GPU head pod);
   and `build_app(...)` leaves non-picklable thread locks in the module
   namespace (cloudpickle can't ship the deployment class to replicas).

Ray Serve's `__call__` pattern (upstream `vllm_example.py`) avoids both:
the proxy is fully vLLM-agnostic, just forwarding URL-prefix-matched
requests to this deployment via Ray actor RPC. All vLLM imports and
state live on the replica, which has the GPU.

To still get vLLM's full OpenAI surface (rather than reinventing the
HTTP contract per the upstream example), we build vLLM's FastAPI app
inside the replica `__init__` and dispatch every request into it via
direct ASGI calls. Streaming is preserved through `StreamingResponse`,
which is Ray Serve's canonical streaming-from-__call__ pattern
(sidesteps the "ASGI callable returned without starting response"
gotcha discussed in ray-project/ray#46966).

The TP > 1 footgun
------------------
With vLLM v1's default Ray executor, vLLM tries to create a nested
placement group inside the one Ray Serve already allocated for the
replica actor — replicas hang in STARTING with no clear error. Fix is
to force the multiprocessing executor BEFORE constructing the engine:
`engine_args.distributed_executor_backend = "mp"`. We set this
unconditionally here so future TP > 1 migrations don't re-hit it.
See vllm-project/vllm#30016, ray-project/ray#59064.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ray import serve
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.types import ASGIApp
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.entrypoints.openai.api_server import (
    build_app,
    build_async_engine_client_from_engine_args,
    init_app_state,
)
from vllm.entrypoints.openai.cli_args import make_arg_parser

# vllm.utils became a package in 0.19; FlexibleArgumentParser is now in
# the argparse_utils submodule. The flat `from vllm.utils import …` path
# the research write-up used no longer resolves.
from vllm.utils.argparse_utils import FlexibleArgumentParser

_log = logging.getLogger(__name__)

def _resolve_model_source(cli_args: dict[str, Any]) -> dict[str, Any]:
    """Mirror Ray Serve LLM's model_source convention for plain vLLM.

    `model_source` is not a vLLM CLI flag. Ray Serve LLM (which we are
    deliberately not using — see module docstring) takes a `model_source`
    dict with `bucket_uri` and quietly mirrors the bucket to a local
    path before invoking the engine. We replicate the behavior here so
    `serveModels.<name>.vllmArgs.model_source: gs://...` keeps working.

    Thin wrapper: the actual resolution lives in `model_source.py`
    (fsspec-based — gs://, s3://, az://, abfs://, hf://, local paths;
    see that module for the supported schemes and caching semantics).
    Remote sources are mirrored under /tmp/models, idempotently: files
    already present at the expected size are skipped, so a replica
    restart in the same pod reuses the cache.

    After this returns, the cli_args dict has `model_source` removed and
    `model` rewritten to the local path (or untouched if model_source
    was absent).
    """
    cli_args = dict(cli_args or {})
    source = cli_args.pop("model_source", None)
    if not source:
        return cli_args

    # Lazy import so the build-time `import vllm_serve_app` smoke check
    # in the Dockerfile stays dependency-light.
    from model_source import resolve_model_source

    cli_args["model"] = resolve_model_source(source)
    return cli_args


def _parse_vllm_args(cli_args: dict[str, Any]):
    """Flatten a dict of vLLM args into argparse via vLLM's own CLI parser.

    Keys are converted from snake_case to --kebab-case. Booleans become
    flag-only when True (and are skipped when False). Lists/tuples are
    expanded into repeated --flag value pairs. Everything else is
    stringified — vLLM's parser handles type coercion.
    """
    parser = FlexibleArgumentParser(description="vLLM in Ray Serve")
    parser = make_arg_parser(parser)
    argv: list[str] = []
    for k, v in (cli_args or {}).items():
        flag = f"--{k.replace('_', '-')}"
        if isinstance(v, bool):
            if v:
                argv.append(flag)
            continue
        if isinstance(v, (list, tuple)):
            for item in v:
                argv.extend([flag, str(item)])
            continue
        argv.extend([flag, str(v)])
    return parser.parse_args(argv)


async def _asgi_passthrough(asgi_app: ASGIApp, request: Request) -> Response:
    """Bridge a Starlette Request into an ASGI app, returning a StreamingResponse.

    Schedules the wrapped ASGI app to run as a background task, captures
    its `http.response.start` message to extract the status code and
    headers, then yields each `http.response.body` chunk into a
    StreamingResponse. Streaming responses (e.g. /v1/chat/completions
    with stream=true) flow through chunk-by-chunk; non-streaming
    responses arrive in a single chunk.

    Returning a StreamingResponse (rather than a raw async generator)
    is Ray Serve's canonical pattern — it avoids the
    "ASGI callable returned without starting response" bug discussed in
    ray-project/ray#46966.
    """
    loop = asyncio.get_running_loop()
    response_started: asyncio.Future = loop.create_future()
    body_queue: asyncio.Queue = asyncio.Queue()

    # Override scope["app"] so vLLM's handlers see vLLM's FastAPI app
    # (and the state we populated via init_app_state) — not Ray Serve's
    # wrapping app. vLLM route handlers access
    # `request.app.state.engine_client` etc., which resolves through
    # scope["app"], so the scope inherited from Ray Serve's proxy
    # leaks the wrong app/state down to the handlers and they 500 with
    # `'State' object has no attribute 'engine_client'`.
    #
    # Do NOT also override scope["state"]: that field is per-request
    # scratch space, and vLLM handlers write to it
    # (`raw_request.state.request_metadata = ...`). Pointing it at the
    # shared app.state breaks those writes — Starlette's State expects
    # _state to be a fresh dict per request, not another State object.
    # Starlette auto-creates a fresh State() via Request.state's
    # property getter when scope["state"] is absent, which is exactly
    # what we want.
    scope = dict(request.scope)
    scope["app"] = asgi_app
    scope.pop("state", None)  # let Starlette create a fresh per-request State

    async def send(message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type == "http.response.start":
            if not response_started.done():
                response_started.set_result(message)
        elif msg_type == "http.response.body":
            await body_queue.put(message)
        # Lifespan and other message types are ignored — Ray Serve manages
        # actor lifecycle, not vLLM's FastAPI lifespan events.

    async def receive() -> dict[str, Any]:
        return await request.receive()

    task = asyncio.create_task(asgi_app(scope, receive, send))

    try:
        start = await response_started
    except BaseException:
        # vLLM raised before sending headers — cancel the task and let
        # the exception propagate so Ray Serve returns a 500.
        task.cancel()
        raise

    status = start["status"]
    headers = [
        (k.decode("latin-1"), v.decode("latin-1"))
        for k, v in start.get("headers", [])
    ]

    async def stream_body():
        try:
            while True:
                msg = await body_queue.get()
                body = msg.get("body", b"")
                if body:
                    yield body
                if not msg.get("more_body"):
                    break
        finally:
            # Wait for the ASGI task to finish (or be cancelled when
            # the client disconnects). Suppress exceptions raised
            # after headers were already sent — partial response
            # delivered, nothing useful we can do.
            if not task.done():
                try:
                    await task
                except BaseException as exc:  # noqa: BLE001
                    _log.warning(
                        "ASGI task raised after response started: %s",
                        exc,
                    )

    return StreamingResponse(
        stream_body(),
        status_code=status,
        headers=dict(headers),
    )


@serve.deployment(
    name="VLLMOpenAI",
    autoscaling_config={
        "min_replicas": 0,
        "max_replicas": 1,
        "target_ongoing_requests": 5,
        "upscale_delay_s": 30,
        "downscale_delay_s": 600,
    },
    max_ongoing_requests=64,
    ray_actor_options={"num_gpus": 1},
)
class VLLMOpenAIDeployment:
    """One replica == one vLLM engine pinned to TP GPUs in this actor.

    The decorator defaults size for a single-GPU TP=1 model.
    Override per-model from the chart via
    `serveConfigV2.applications[].deployments:` — that's how every
    knob (num_gpus, replica bounds, target_ongoing_requests,
    max_ongoing_requests) reaches this class without an image rebuild.
    """

    def __init__(self, vllm_cli_args: dict[str, Any]):
        # Mirror Ray Serve LLM's model_source → local-path rewrite so
        # `vllmArgs.model_source: gs://...` in values.yaml works without
        # the Ray Serve LLM helper. Runs once per replica (in this
        # actor's __init__ on the worker pod, where open-serve-worker's
        # Workload Identity is bound for GCS access).
        vllm_cli_args = _resolve_model_source(vllm_cli_args)
        args = _parse_vllm_args(vllm_cli_args)
        self._args = args

        # vLLM's full OpenAI-compatible FastAPI app — has every route
        # vLLM registers (chat/completions, embeddings, responses, score,
        # rerank, tokenize, detokenize, pooling, classify, audio, models,
        # health, version, openapi.json). Per-replica object; engine
        # state is injected lazily on the first request.
        self._vllm_app: ASGIApp = build_app(args)

        # See module docstring: forces vLLM's mp executor so TP workers
        # live inside this single Serve actor's bundle instead of
        # trying to create a nested placement group.
        engine_args = AsyncEngineArgs.from_cli_args(args)
        engine_args.distributed_executor_backend = "mp"
        # NOTE: build_async_engine_client_from_engine_args is an
        # @asynccontextmanager that yields the AsyncLLM and calls
        # `await async_llm.shutdown()` in its finally block. The
        # _maintain_engine task below holds it open via `async with`
        # for the lifetime of the replica. See _maintain_engine
        # docstring for why the earlier "__aenter__ and forget"
        # pattern caused the engine to shut down ~26 s after init.
        self._engine_args = engine_args
        self._engine_client = None
        # The lifetime task is created lazily on first __call__
        # (asyncio primitives need a running loop). Single-asyncio-loop
        # scheduling makes the check-and-set in _ensure_engine_ready
        # race-free.
        self._engine_lifetime_task: asyncio.Task | None = None
        self._engine_ready: asyncio.Event | None = None

    async def _ensure_engine_ready(self) -> None:
        """Start (or attach to) the engine-lifetime task; wait until ready.

        This used to live in `reconfigure()`, but Ray Serve only calls
        reconfigure on the initial deployment — not on actor restarts
        (replica process crash + respawn within the same pod). A
        restarted actor with reconfigure-only setup ends up with no
        engine_client on app.state, so the first request returns 500.
        Doing it lazily on the first __call__ guarantees the engine is
        always initialised regardless of how Ray Serve started the
        actor.

        Cancellation safety: vLLM engine init can take 1-2 min for a
        16 GB model. If the first HTTP client cancels mid-init, the
        engine context manager would be left half-entered (`args`
        deleted, no client) and the next caller's __aenter__ would
        crash with AttributeError. We sidestep this by running the
        engine in a long-lived background asyncio.Task whose
        cancellation is decoupled from any individual request — the
        task continues past curl timeouts. All callers just await the
        Event the task sets once init completes.
        """
        if self._engine_client is not None:
            return
        # Single-threaded asyncio: no preemption between the check and
        # the assignment below, so this lazy-init is race-free.
        if self._engine_lifetime_task is None:
            self._engine_ready = asyncio.Event()
            self._engine_lifetime_task = asyncio.create_task(self._maintain_engine())

        ready_task = asyncio.ensure_future(self._engine_ready.wait())
        try:
            done, _ = await asyncio.wait(
                {ready_task, self._engine_lifetime_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not ready_task.done():
                ready_task.cancel()
        # If the lifetime task ended without setting ready, init failed —
        # surface the exception so the request returns 5xx instead of
        # hanging forever.
        if self._engine_lifetime_task in done and not self._engine_ready.is_set():
            self._engine_lifetime_task.result()  # raises

    async def _maintain_engine(self) -> None:
        """Hold the engine context manager open for the replica's lifetime.

        The vLLM engine context manager (`build_async_engine_client_from
        _engine_args`) is an @asynccontextmanager that constructs an
        AsyncLLM at `__aenter__` and calls `async_llm.shutdown()` in
        the `finally` after the yield. The previous "__aenter__ and
        store the client" pattern caused vLLM to shut down its
        EngineCore subprocess ~26 s after init completed — observed in
        dev with replica reported HEALTHY by Ray Serve but every
        request CANCELLED at the ASGI dispatch because the underlying
        engine subprocess was gone. Suspected cause: the asyncgen
        backing the context manager has a finalizer (Python's default
        asyncgen GC hooks) that runs aclose() and triggers the
        finally cleanup once the task that called `__aenter__` is no
        longer iterating it.

        Fix: keep the `async with` block active for the entire actor
        lifetime by sleeping forever inside it. The CM is torn down
        cleanly when this task is cancelled at actor teardown.
        """
        async with build_async_engine_client_from_engine_args(self._engine_args) as engine_client:
            await init_app_state(
                engine_client=engine_client,
                state=self._vllm_app.state,
                args=self._args,
            )
            self._engine_client = engine_client
            self._engine_ready.set()
            # Block forever until Ray Serve cancels this task at
            # replica teardown. The cancellation propagates through
            # the `async with` to the CM's __aexit__, which shuts down
            # the engine gracefully.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Re-raise so __aexit__ runs the engine shutdown.
                raise

    async def __call__(self, request: Request) -> Response:
        """Dispatch every incoming request into vLLM's FastAPI app via ASGI.

        Ray Serve's HTTP proxy routes by `route_prefix` only — it does
        not need to know individual vLLM routes. The deployment's
        `__call__` runs on the replica (this worker pod), where
        `self._vllm_app` already has the full route table and engine
        state. _asgi_passthrough handles streaming.
        """
        await self._ensure_engine_ready()
        return await _asgi_passthrough(self._vllm_app, request)


def build_app_fn(cli_args: dict[str, Any]) -> serve.Application:
    """Entry point referenced by serveConfigV2 import_path.

    Runs on the Ray Serve controller (head pod). Returns a serve
    Application without importing anything heavyweight — vLLM's engine
    is constructed per-replica in VLLMOpenAIDeployment.__init__, not
    here. That keeps the controller (CPU-only) vLLM-agnostic and
    avoids the DeviceConfig auto-detect failure that would otherwise
    fire on a host without a GPU.
    """
    return VLLMOpenAIDeployment.bind(cli_args or {})
