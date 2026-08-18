# Plain Ray Serve + vLLM image for open-serve — the `vllm-raw` runner.
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
RUN pip install --no-cache-dir \
      "vllm[audio]==0.19.1" \
      "transformers==5.5.1" \
      "fastapi>=0.115" \
      "uvicorn>=0.30" \
      "pandas>=2.2.0" \
      "google-cloud-storage>=2.18.0" \
    && pip cache purge \
    && python -c "import vllm; import transformers; from google.cloud import storage; print('vllm', vllm.__version__, 'transformers', transformers.__version__, 'storage', storage.__version__)"

# Bake the Ray Serve entrypoint module into the image so the chart's
# serveConfigV2 can reference `vllm_serve_app:build_app_fn` without
# mounting a ConfigMap. Sits on PYTHONPATH alongside /app/serve-scripts
# (the ConfigMap mount path for type=ray-serve-llm/custom models).
COPY vllm_serve_app.py /home/ray/vllm_serve_app.py
ENV PYTHONPATH=/home/ray:/app/serve-scripts

# Fail fast at build time if the module can't import (cheap CI guard).
RUN python -c "import vllm_serve_app; assert hasattr(vllm_serve_app, 'build_app_fn'), 'build_app_fn missing'"

# Recommended runtime defaults for vLLM v1 in containers.
ENV VLLM_USE_V1=1 \
    VLLM_WORKER_MULTIPROC_METHOD=spawn
