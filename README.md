# open-serve

**An open, batteries-included model-serving framework for Kubernetes, built on Ray Serve, Ray Serve LLM, and vLLM.**

open-serve turns a Kubernetes cluster into a production model-serving platform. Flip `enabled: true` on models from a curated catalog and get an OpenAI-compatible endpoint with:

- **Per-model Ray Serve deployments** (KubeRay RayServices) with zero-downtime rollouts and scale-to-zero autoscaling
- **API-key authentication and routing** via a lightweight gateway with per-source/model/org usage metrics
- **Observability out of the box** — Grafana dashboards (Ray, Serve, vLLM, model comparison, SLO), Prometheus alert rules, synthetic end-to-end probes
- **A public status page** (huggingface.co/openai-status style) driven by probe metrics, with latency-aware SLO classification
- **Cost tracking** — per-model GPU-hour and token attribution *(roadmap)*
- **GitOps-native deployment** — FluxCD reference layout with kustomize base + environment overlays

GCP/GKE is the first supported provider. AWS and Azure are on the roadmap.

> **Status: pre-release.** The initial extraction is under active development; expect breaking changes until v0.1.0.

## Architecture

```
Client
   │  Authorization: Bearer sk-<source>-<hex>
   ▼
Gateway API (TLS)  ─or─  internal LB
   │
   ▼
open-serve-gateway
   ├─ validates Bearer token against key map
   ├─ records openserve_requests_total{source, model, org}
   └─ routes by path-prefix and/or model name
        ├─ /v1/chat/completions  → rayservice-<model>-serve-svc  (per model)
        ├─ /v1/embeddings        → rayservice-<embed-model>-serve-svc
        └─ /tokenize, /detokenize, /v1/responses, …  (vllm-raw models)
```

Each model runs as its **own RayService**, so models scale, roll out, and fail independently.

## Runners

| `type:` | Use when | API surface |
|---|---|---|
| `vllm-raw` *(default)* | Full vLLM OpenAI API needed | Everything vLLM serves: `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, `/tokenize`, `/detokenize`, `/v1/score`, `/v1/rerank`, audio |
| `ray-serve-llm` | Ray Serve LLM's `LLMConfig` + `build_openai_app` flow | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `custom` | Non-LLM models (rerankers, OCR, CNNs) | Your own serve script / import path |

Models needing `/tokenize` and `/detokenize` should be deployed as `vllm-raw` — those endpoints come natively from vLLM.

## Repository layout

```
charts/open-serve/     # the serving Helm chart
services/
  gateway/             # auth + routing + usage metrics
  probe/               # synthetic end-to-end prober
  status/              # public status page
runtimes/
  vllm/                # vllm-raw runtime (ASGI passthrough to vLLM's OpenAI app)
  ray-serve-llm/       # ray-serve-llm runtime image
catalog/models/        # curated model presets
deploy/flux/           # GitOps reference (FluxCD + kustomize)
terraform/gcp/         # optional reference infrastructure (GKE + GPU pools)
examples/              # quickstarts (kind, GCP)
docs/                  # documentation
scripts/               # operational tooling (connect, smoke tests, load tests)
```

## Quickstart

Coming with v0.1.0 — a kind-based CPU demo and a GCP end-to-end guide. Track progress in the issues.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Adding support for a newly released open-weights model is the easiest way to contribute — model presets live in `catalog/models/`.

## License

[Apache-2.0](LICENSE)
