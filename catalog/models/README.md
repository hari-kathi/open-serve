# Model catalog

Curated, tested presets for open-weights models. Each preset is a YAML fragment that plugs into the chart's `serveModels` map: tested `vllmArgs`, GPU sizing per accelerator type, context lengths, and probe metadata.

Enable a model by merging its preset into your environment values and setting `enabled: true`; disable it with `enabled: false` (the RayService is removed without touching other models).

**Status:** catalog v1 (~8–10 presets: gpt-oss, Llama 3.x, Qwen3 / Qwen3-VL, Gemma, DeepSeek-R1 distill, Mistral, mxbai / bge-m3 embeddings, bge-reranker) lands before v0.1.0. See `example-qwen3-8b.yaml` for the preset shape.

Contributing a preset for a newly released model is the easiest way to contribute — see CONTRIBUTING.md.
