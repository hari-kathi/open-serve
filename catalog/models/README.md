# Model catalog

Curated presets for open-weights models. Each preset is a YAML fragment that plugs into the chart's `serveModels` map: tested `vllmArgs`, GPU sizing per accelerator type, context lengths, and probe metadata (`tier`, `runner`, `category`).

Enable a model by merging its preset into your environment values under `serveModels:` and setting `enabled: true`; disable it with `enabled: false` (the RayService is removed without touching other models). All presets ship `enabled: false`, `tier: production`, `type: vllm-raw`, and scale-to-zero replicas (`min: 0, max: 1`).

GPU sizing in each preset header is indicative — validate for your accelerator, driver stack, and quota before promoting to production. Each preset's `vllmArgs` also documents the three weight-source options: pull from the Hugging Face Hub by model id, or pre-staged `gs://` / `s3://` buckets via `model_source`.

## Presets (catalog v1)

| Preset | Model | Runner | Min GPUs (default config) | Context (as configured) |
|---|---|---|---|---|
| [`gpt-oss-20b.yaml`](gpt-oss-20b.yaml) | openai/gpt-oss-20b | chat | 1x A100-80GB (MXFP4) | 131,072 |
| [`gpt-oss-120b.yaml`](gpt-oss-120b.yaml) | openai/gpt-oss-120b | chat | 2x A100-80GB or 1x H100-80GB (MXFP4) | 131,072 |
| [`llama-3-1-8b-instruct.yaml`](llama-3-1-8b-instruct.yaml) | meta-llama/Llama-3.1-8B-Instruct | chat | 1x A100-40GB | 32,768 |
| [`llama-3-3-70b-instruct.yaml`](llama-3-3-70b-instruct.yaml) | meta-llama/Llama-3.3-70B-Instruct | chat | 4x A100-40GB or 2x A100-80GB | 8,192 |
| [`qwen3-8b.yaml`](qwen3-8b.yaml) | Qwen/Qwen3-8B | chat | 1x A100-40GB | 32,768 |
| [`qwen3-32b.yaml`](qwen3-32b.yaml) | Qwen/Qwen3-32B | chat | 1x A100-80GB or 2x A100-40GB | 32,768 |
| [`qwen3-vl-8b-instruct.yaml`](qwen3-vl-8b-instruct.yaml) | Qwen/Qwen3-VL-8B-Instruct | chat (Vision-Language) | 1x A100-40GB | 32,768 |
| [`gemma-3-27b-it.yaml`](gemma-3-27b-it.yaml) | google/gemma-3-27b-it | chat | 1x A100-80GB or 2x A100-40GB | 32,768 |
| [`deepseek-r1-distill-llama-8b.yaml`](deepseek-r1-distill-llama-8b.yaml) | deepseek-ai/DeepSeek-R1-Distill-Llama-8B | chat | 1x A100-40GB | 32,768 |
| [`mistral-small-24b-instruct.yaml`](mistral-small-24b-instruct.yaml) | mistralai/Mistral-Small-24B-Instruct-2501 | chat | 1x A100-80GB or 2x A100-40GB | 32,768 |
| [`mxbai-embed-large-v1.yaml`](mxbai-embed-large-v1.yaml) | mixedbread-ai/mxbai-embed-large-v1 | embedding | 1x L4-24GB | 512 |
| [`bge-m3.yaml`](bge-m3.yaml) | BAAI/bge-m3 | embedding | 1x L4-24GB | 8,192 |

Notes:

- **Gated models** — `llama-3-1-8b-instruct`, `llama-3-3-70b-instruct`, and `gemma-3-27b-it` require accepting the license on Hugging Face and an `HF_TOKEN` env entry (commented example in each preset), unless weights are pre-staged via `model_source`.
- **gpt-oss** — ships native MXFP4 quantization; presets deliberately omit `dtype: bfloat16` (all other presets set it).
- **Embeddings** — served through vLLM's pooling runner (`vllmArgs.runner: pooling`); reach them at `/v1/embeddings` behind a dedicated gateway path prefix.

Every preset must render through the chart: run `scripts/validate-catalog.sh` after adding or editing one. See `qwen3-8b.yaml` for the canonical preset shape. Contributing a preset for a newly released model is the easiest way to contribute — see CONTRIBUTING.md.
