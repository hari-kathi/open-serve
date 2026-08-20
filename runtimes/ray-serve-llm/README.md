# `ray-serve-llm` runtime — the `ray-serve-llm` runner image

This image backs open-serve's **`ray-serve-llm`** runner: models whose serve
script uses Ray Serve LLM's high-level API —

```python
from ray.serve.llm import LLMConfig, build_openai_app
```

`build_openai_app` exposes the standard OpenAI surface only:
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models`.
If a model needs the full vLLM endpoint surface (`/v1/responses`,
`/tokenize`, `/detokenize`, `/v1/score`, `/v1/rerank`, audio), use the
`vllm` runner (`runtimes/vllm/`) instead.

## Build

```bash
docker build --platform linux/amd64 -t <registry>/open-serve-ray-serve-llm:<tag> runtimes/ray-serve-llm/
```

## Alternative: upstream `rayproject/ray-llm` images

You can often skip building this image entirely and point
`serveModels.<name>.image` at an upstream `rayproject/ray-llm` image, which
bundles ray.serve.llm plus a curated vLLM. This custom build exists for cases
where you want to control the exact Ray + vLLM pairing yourself (e.g. when an
upstream release regresses a feature your models depend on, such as
`model_source: bucket_uri` handling).

## Version-matrix caveat

Ray **2.53.0** + vLLM **0.13.0** is a proven combination. Do **not** bump Ray
or vLLM independently of each other — `ray.serve.llm` is tightly coupled to
the vLLM version it was released against, and mismatched pairs fail in
non-obvious ways (import errors at best, silent behavior drift at worst).
Upgrade both together to a pairing you have validated end-to-end (the
build-time `from ray.serve.llm import LLMConfig, build_openai_app` assertion
catches only the grossest breakage).
