# Ray Serve LLM base image for open-serve — the `ray-serve-llm` runner.
#
# Built on the plain rayproject/ray base with a pinned vLLM installed on
# top. Uses the ray.serve.llm LLMConfig + build_openai_app API.
#
# Build context: runtimes/ray-serve-llm/
#   docker build --platform linux/amd64 -t <tag> runtimes/ray-serve-llm/
FROM --platform=linux/amd64 rayproject/ray:2.53.0-py311-cu121

LABEL description="Ray Serve LLM runtime for open-serve (LLMConfig + build_openai_app)"

# Ray 2.53.0 + vllm 0.13.0 is a proven combination.
# vllm 0.13.0 may upgrade numpy; include pandas>=2.0 for ABI compat.
RUN pip install --no-cache-dir \
    "ray[serve]==2.53.0" \
    "vllm==0.13.0" \
    "pandas>=2.0" \
    "numpy>=1.26.0" \
    && pip cache purge \
    && python -c "from ray.serve.llm import LLMConfig, build_openai_app; print('OK')"
