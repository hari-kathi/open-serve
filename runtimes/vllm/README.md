# `vllm` runtime — the `vllm-raw` runner image

This image backs open-serve's **`vllm-raw`** runner: a plain Ray Serve
deployment (`vllm_serve_app.py`, baked into the image) that dispatches every
incoming request into vLLM's own OpenAI-compatible FastAPI app via the ASGI
protocol — a `__call__` + ASGI passthrough, not `@serve.ingress`.

Because the passthrough mounts vLLM's *full* route table, everything vLLM
serves is reachable end-to-end, including endpoints that Ray Serve LLM's
`build_openai_app` does not expose:

- `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`
- `/v1/responses` (OpenAI Responses API — Agents SDK compatible)
- `/tokenize`, `/detokenize`
- `/v1/score`, `/v1/rerank`, `/pooling`, `/classify`
- `/v1/audio/*` (transcriptions; the `vllm[audio]` extra is installed)

The chart references the module as `import_path: vllm_serve_app:build_app_fn`
in each model's `serveConfigV2`, forwarding `vllmArgs` from values.yaml as the
app `args`. See the module docstring in `vllm_serve_app.py` for the full
serveConfigV2 shape and the design rationale.

## Build

```bash
docker build --platform linux/amd64 -t <registry>/open-serve-vllm:<tag> runtimes/vllm/
```

## Pinned version matrix

| Component | Version | Why pinned |
|---|---|---|
| `rayproject/ray` (base) | `2.55.1-py311-cu128` | Ray Serve base the module was validated against |
| `vllm[audio]` | `0.19.1` | Internal-ish vLLM APIs used (`build_app`, `init_app_state`, `make_arg_parser`) drift across minors; 0.19.1 is the validated version |
| `transformers` | `5.5.1` | Matched to vllm 0.19.1's model registry |
| `pandas` | `>=2.2.0` | Forces a numpy-2-ABI wheel; newer vllm pulls numpy 2.x, and the base image's pandas (numpy-1 ABI) breaks `import ray.serve` at build time |
| `google-cloud-storage` | `>=2.18.0` | `model_source: gs://...` mirroring in `_resolve_model_source` |

Do not bump vLLM or Ray independently — treat the matrix as one unit and
re-validate the build-time import assertion plus a real model deployment when
upgrading.

## Engine-lifecycle warning (do not refactor)

The engine lifecycle in `vllm_serve_app.py` deliberately holds vLLM's
`build_async_engine_client_from_engine_args(...)` context manager open inside
a long-lived background `asyncio.Task` (`_maintain_engine`), which sleeps
forever inside the `async with` block until the task is cancelled at replica
teardown.

**Never refactor this to a plain `__aenter__`/`__aexit__` ("enter and store
the client") pattern.** The context manager is an `@asynccontextmanager`
whose backing async generator gets finalized by Python's default asyncgen GC
hooks once the task that entered it stops iterating — which runs the
`finally` block and calls `await async_llm.shutdown()`. In practice this
killed the vLLM EngineCore subprocess ~26 seconds after init: Ray Serve
reported the replica HEALTHY while every request was CANCELLED at ASGI
dispatch because the engine was gone. The `asyncio.Task` + held-open
`async with` pattern is the fix; cancellation at teardown propagates through
the context manager and shuts the engine down cleanly.
