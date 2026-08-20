# Runners

Every `serveModels` entry declares a `type:` that selects one of three runners. The runner determines which serving code runs inside the RayService — and therefore which HTTP endpoints the model exposes.

## Decision table

| `type:` | Use when | API surface |
|---|---|---|
| `vllm` *(default)* | Any OpenAI-compatible model — chat, completions, embeddings (via pooling), multimodal | Everything vLLM serves: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`, `/v1/responses`, `/tokenize`, `/detokenize`, `/v1/score`, `/v1/rerank`, `/pooling`, `/classify`, `/v1/audio/*` |
| `ray-serve-llm` | You want Ray Serve LLM's `LLMConfig` + `build_openai_app` flow | The subset `build_openai_app` proxies: `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `custom` | Non-LLM models — rerankers, classifiers, OCR, CNNs | Whatever your own serve script exposes |

!!! note "`/tokenize` and `/detokenize`"
    Every `vllm` model serves `/tokenize` and `/detokenize` natively — they come from vLLM's own FastAPI app, and the gateway routes them like any other endpoint: by the request body's `model` field. `ray-serve-llm` models do **not** serve them: the gateway still routes the request to the model's backend, but the backend answers `404`. If your consumers need `/tokenize`/`/detokenize` (or `/v1/responses`, score/rerank, audio), prefer `type: vllm`.

## `vllm`

Backed by the `runtimes/vllm/` image (`open-serve-vllm`). It drops `ray.serve.llm` entirely: a plain Ray Serve deployment (`vllm_serve_app.py`, baked into the image on `PYTHONPATH`) dispatches every incoming request into vLLM's own OpenAI-compatible FastAPI app via ASGI passthrough. Because the passthrough mounts vLLM's *full* route table, every route vLLM registers is reachable end to end.

Configuration is pure values.yaml — no serve script to write:

```yaml
serveModels:
  qwen3-8b:
    enabled: true
    type: vllm
    modelId: "Qwen/Qwen3-8B"
    image:
      repository: "hari-kathi/open-serve-vllm"   # override the chart default image
      tag: "0.1.0"
    vllmArgs:                    # any vLLM CLI flag, snake_case
      model: "Qwen/Qwen3-8B"
      served_model_name: "Qwen/Qwen3-8B"
      dtype: "bfloat16"
      max_model_len: 32768
      enable_auto_tool_choice: true
      tool_call_parser: "hermes"
```

Every key under `vllmArgs` forwards into vLLM's own argparse (`snake_case` → `--kebab-case`; booleans become flag-only when true; dicts must be JSON strings). Ray Serve deployment options (replica bounds, `num_gpus`, autoscaling) come from the standard `replicas` / `autoscaling` / `gpu` fields — the chart emits a `deployments:` block targeting the `VLLMOpenAI` deployment.

Embedding models are `vllm` models too: vLLM serves them through its pooling runner (e.g. `vllmArgs: { task: embed }` for models like `mxbai-embed-large-v1`), exposing the standard `/v1/embeddings` endpoint.

## `ray-serve-llm`

Backed by the `runtimes/ray-serve-llm/` image (`open-serve-ray-serve-llm`). You provide an inline `serveScript` (or `serveScriptFile`) that builds the app with Ray Serve LLM's high-level API:

```python
from ray.serve.llm import LLMConfig, build_openai_app
```

The chart injects the script into a ConfigMap mounted at `/app/serve-scripts` and imports it as `serve_<name>:app`. You get Ray Serve LLM's engine management and its standard OpenAI subset — `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` — nothing beyond it.

You can often skip building the image and point `serveModels.<name>.image` at an upstream `rayproject/ray-llm` image instead; the custom build exists for controlling the exact Ray + vLLM pairing.

## `custom`

For everything that isn't an LLM. Provide an `importPath` (plus optionally `serveScript`/`serveScriptFile`) pointing at your own Ray Serve application. The chart still gives you the full per-model machinery: an isolated RayService, autoscaling bounds, optional GPU (or `gpu.count: 0` for CPU-only), ConfigMap-injected code, and pod labels for observability.

The probe's `runner` field is open-ended for the same reason: `chat` and `embedding` get tailored functional probes; any other runner string falls back to Ray Serve's generic `/-/healthy` liveness probe until a tailored handler is added (see the extension contract in `services/probe/main.py`).

## Version matrices — do not bump independently

Both runtime images pin a Ray + vLLM pairing that was validated end to end. Treat each matrix as one unit.

**`vllm` (`runtimes/vllm/`):** Ray `2.55.1` base + vLLM `0.19.1` (+ matched `transformers`). The module uses internal-ish vLLM APIs (`build_app`, `init_app_state`, `make_arg_parser`) that drift across vLLM minors. Re-validate the build-time import assertion *and* a real model deployment when upgrading.

**`ray-serve-llm` (`runtimes/ray-serve-llm/`):** Ray `2.53.0` + vLLM `0.13.0` is the proven combination. `ray.serve.llm` is tightly coupled to the vLLM version it was released against; mismatched pairs fail as import errors at best and silent behavior drift at worst.

Set `rayVersion` on the model entry to match the image you deploy with (e.g. `rayVersion: "2.55.1"` for the vllm image).

## Choosing quickly

- Any OpenAI-compatible model, and you want the whole surface (Responses API, tokenize, score/rerank, audio) or embeddings via pooling? → **`vllm`**
- LLM, the chat/completions/embeddings subset is enough, and you prefer `LLMConfig`-style config? → **`ray-serve-llm`**
- Not an LLM (reranker, classifier, OCR, CNN)? → **`custom`**
