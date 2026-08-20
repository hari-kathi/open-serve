# Plain Ray Serve + vLLM image for open-serve — the `vllm` runner.
#
# Unlike the ray-serve-llm runtime (built on rayproject/ray-llm, which bundles
# ray.serve.llm + a curated vLLM), this image is built on the non-LLM
# rayproject/ray base and installs vLLM + transformers directly. The
# motivation is the vLLM OpenAI-compatible API surface: ray.serve.llm's
# build_openai_app only exposes chat/completions/embeddings, while plain
# Ray Serve dispatching into vLLM's own FastAPI app exposes every route
# vLLM registers — /v1/responses, /v1/score, /v1/rerank, /tokenize,
# /detokenize, /pooling, /classify, audio transcriptions, etc.
#
# Build context: runtimes/vllm/
#   docker build --platform linux/amd64 -t <tag> runtimes/vllm/
FROM --platform=linux/amd64 rayproject/ray:2.55.1-py311-cu128

LABEL description="Plain Ray Serve + vLLM runtime for open-serve (no ray.serve.llm)"

# vLLM 0.19.1 + transformers 5.5.1 is the validated combination for this
# runner. The [audio] extra pulls in librosa/soundfile so audio encoders work.
#
# Pins (vs. `>=` ranges) for two reasons:
# 1. The vLLM helper APIs we use (`build_app`, `init_app_state`,
#    `make_arg_parser`) are internal-ish and have changed across vLLM
#    minor versions. This module was validated against 0.19.1;
#    later versions risk silent API drift.
# 2. The first build of this image with `>=0.19.1` pulled vllm 0.21.0,
#    which dragged numpy from the base image's 1.26.4 up to 2.3.5. The
#    base image's pre-installed pandas was compiled against numpy 1.x
#    ABI, so `import ray.serve` (→ `import pandas`) failed at build time
#    with "numpy.dtype size changed". Explicit pandas>=2.2.0 forces
#    pandas onto a numpy-2-ABI wheel.
#
# model_source.py resolver stack (replaces the old google-cloud-storage
# gs://-only mirror — google-cloud-storage is dropped; gcsfs is the GCS
# driver now):
# - fsspec is pinned to 2026.7.0 because s3fs hard-pins its matching
#   fsspec release (`fsspec>=2026.7.0,<2026.7.1`); gcsfs 2026.8.0
#   (fsspec>=2026.7.0) and adlfs 2026.8.0 (fsspec>=2023.12.0) both
#   accept it.
# - huggingface_hub: vllm 0.19.1 has no direct huggingface_hub dep; it
#   arrives transitively via transformers 5.5.1, which constrains it to
#   >=1.5.0,<2.0 — we mirror that constraint rather than fight it.
#   HF_TOKEN is honored implicitly by snapshot_download for gated repos.
RUN pip install --no-cache-dir \
      "vllm[audio]==0.19.1" \
      "transformers==5.5.1" \
      "fastapi>=0.115" \
      "uvicorn>=0.30" \
      "pandas>=2.2.0" \
      "fsspec==2026.7.0" \
      "gcsfs==2026.8.0" \
      "s3fs==2026.7.0" \
      "adlfs==2026.8.0" \
      "huggingface_hub>=1.5.0,<2.0" \
    && pip cache purge \
    && python -c "import vllm; import transformers; import fsspec, gcsfs, s3fs, adlfs, huggingface_hub; print('vllm', vllm.__version__, 'transformers', transformers.__version__, 'fsspec', fsspec.__version__, 'hf_hub', huggingface_hub.__version__)"

# Bake the Ray Serve entrypoint module into the image so the chart's
# serveConfigV2 can reference `vllm_serve_app:build_app_fn` without
# mounting a ConfigMap. Sits on PYTHONPATH alongside /app/serve-scripts
# (the ConfigMap mount path for type=ray-serve-llm/custom models).
# model_source.py is its fsspec-based model_source resolver.
COPY vllm_serve_app.py /home/ray/vllm_serve_app.py
COPY model_source.py /home/ray/model_source.py
ENV PYTHONPATH=/home/ray:/app/serve-scripts

# Fail fast at build time if the modules can't import (cheap CI guard).
RUN python -c "import vllm_serve_app; assert hasattr(vllm_serve_app, 'build_app_fn'), 'build_app_fn missing'; import model_source; assert hasattr(model_source, 'resolve_model_source')"

# Recommended runtime defaults for vLLM v1 in containers.
ENV VLLM_USE_V1=1 \
    VLLM_WORKER_MULTIPROC_METHOD=spawn
