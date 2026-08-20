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

> **Current release: [v0.2.0](https://github.com/hari-kathi/open-serve/releases)** — images and the Helm chart are published to GHCR. Pre-1.0, minor versions may include breaking changes; see release notes when upgrading.

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
   └─ routes every request by the body's "model" field (gateway.modelRoutes)
        ├─ model: Qwen/Qwen3-8B         → rayservice-qwen3-8b-serve-svc
        ├─ model: mxbai-embed-large-v1  → rayservice-mxbai-embed-serve-svc
        └─ chat, embeddings, /tokenize, /detokenize, /v1/responses, … alike
```

Each model runs as its **own RayService**, so models scale, roll out, and fail independently.

## Runners

| `type:` | Use when | API surface |
|---|---|---|
| `vllm` *(default)* | Any OpenAI-compatible model — chat, completions, embeddings, multimodal | Everything vLLM serves: `/v1/chat/completions`, `/v1/responses`, `/v1/embeddings`, `/tokenize`, `/detokenize`, `/v1/score`, `/v1/rerank`, audio |
| `ray-serve-llm` | Ray Serve LLM's `LLMConfig` + `build_openai_app` flow | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `custom` | Non-LLM models (rerankers, OCR, CNNs) | Your own serve script / import path |

Every `vllm` model serves `/tokenize` and `/detokenize` natively — the gateway routes them by the request body's `model` field like every other endpoint. `ray-serve-llm` models don't serve them.

## Repository layout

```
charts/open-serve/     # the serving Helm chart
services/
  gateway/             # auth + routing + usage metrics
  probe/               # synthetic end-to-end prober
  status/              # public status page
runtimes/
  vllm/                # vllm runtime (ASGI passthrough to vLLM's OpenAI app)
  ray-serve-llm/       # ray-serve-llm runtime image
catalog/models/        # curated model presets
deploy/flux/           # GitOps reference (FluxCD + kustomize)
terraform/gcp/         # optional reference infrastructure (GKE + GPU pools)
examples/              # quickstarts (kind, GCP)
docs/                  # documentation
scripts/               # operational tooling (connect, smoke tests, load tests)
```

## Deploy it

Full guides at **[hari-kathi.github.io/open-serve](https://hari-kathi.github.io/open-serve/)** — written for anyone with basic Kubernetes/Terraform familiarity, validated command-by-command on real accounts:

- **[Local on kind](docs/quickstart/kind.md)** — the full stack with a CPU demo model in ~10 minutes; no cloud account or GPUs (`examples/kind-local/run.sh`)
- **[GCP / GKE](docs/quickstart/gcp.md)** — Terraform reference to a GPU-ready cluster (`examples/gcp-quickstart/`), then models from the catalog
- **[AWS / EKS](docs/quickstart/aws.md)** — same, on EKS with IRSA (`examples/aws-quickstart/`)

Both cloud guides lead with the preflight scripts (`scripts/gcp-preflight.sh`, `scripts/aws-preflight.sh`) that check the zero-by-default GPU quotas every new account hits.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Adding support for a newly released open-weights model is the easiest way to contribute — model presets live in `catalog/models/`.

## License

[AGPL-3.0](LICENSE)
