# Documentation

The docs site (mkdocs-material) lands before v0.1.0. Planned structure:

- **Quickstart** — kind (CPU-only demo), then GCP end-to-end
- **Concepts** — architecture, the three runners (`vllm-raw`, `ray-serve-llm`, `custom`), model lifecycle
- **Model catalog** — preset reference and contribution guide
- **Operations** — adding a model, rollback, scaling, API-key rotation, probe/status/SLO configuration
- **Cloud guides** — GCP first; AWS and Azure to follow
- **API reference** — OpenAI-compatible endpoints per runner (note: `/tokenize` and `/detokenize` require `vllm-raw`)
