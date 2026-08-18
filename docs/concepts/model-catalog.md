# Model catalog

`catalog/models/` holds curated, tested presets for open-weights models. A preset is a YAML fragment that plugs straight into the chart's `serveModels` map: tested `vllmArgs`, GPU sizing per accelerator type, context lengths, and probe metadata. The goal is that serving a well-known model is a merge + `enabled: true`, not a tuning project.

!!! note "Catalog status"
    Catalog v1 (~8–10 presets: gpt-oss, Llama 3.x, Qwen3 / Qwen3-VL, Gemma, DeepSeek-R1 distill, Mistral, mxbai / bge-m3 embeddings, bge-reranker) lands before v0.1.0. Check `catalog/models/` for what exists today.

## Anatomy of a preset

Presets are keyed by a short model name and follow the `serveModels` schema (`catalog/models/example-qwen3-8b.yaml` is the reference shape):

```yaml
qwen3-8b:
  enabled: false                    # you flip this in your environment values
  modelId: "Qwen/Qwen3-8B"          # the id clients pass as "model" and the probe asserts
  tier: production                  # production → probed + SLO-gated alerts; internal-test → not probed
  runner: chat                      # probe dispatch: chat | embedding | anything else
  category: LLM                     # status-page section (LLM, Embedding, Reranker, ...)
  type: vllm-raw                    # which runner executes the model (see Runners)
  routePrefix: "/"
  sharedMemorySize: "20Gi"          # /dev/shm for tensor-parallel workers
  vllmArgs:                         # tested engine flags — the heart of the preset
    model: "Qwen/Qwen3-8B"
    served_model_name: "Qwen/Qwen3-8B"
    tensor_parallel_size: 1
    dtype: "bfloat16"
    max_model_len: 32768
    max_num_seqs: 64
    gpu_memory_utilization: 0.90
    enable_auto_tool_choice: true
    tool_call_parser: "hermes"
  replicas: { min: 0, max: 1 }      # Serve autoscaling bounds
  autoscaling:
    targetOngoingRequests: 5
    downscaleDelayS: 600
    upscaleDelayS: 60
  gpu:
    count: 1
    acceleratorType: "A100"         # Ray routes replicas to matching workers
  resources:
    worker:
      requests: { cpu: "8", memory: "32Gi" }
      limits: { cpu: "8", memory: "48Gi" }
```

The fields split into three concerns:

- **Serving** — `type`, `vllmArgs` (or `serveScript`/`importPath` for the other runners), `routePrefix`, image override.
- **Capacity** — `replicas`, `autoscaling`, `gpu`, `resources`, `sharedMemorySize`. Presets include GPU sizing notes (e.g. "1x A100 40GB, or L4-class with reduced `max_model_len`").
- **Operations metadata** — `tier`, `runner`, `category`, `description`: these drive the probe's target list, alert gating, and the status page's rows and sections.

## Enabling and disabling a model

Merge the preset into your environment values (for Flux deployments, the overlay's `open-serve/values.yaml`) under `serveModels:` and set `enabled: true`:

```yaml
serveModels:
  qwen3-8b:
    enabled: true
    # ...rest of the preset...
```

Deploy (merge to your GitOps branch, or `helm upgrade`). The chart renders one RayService per enabled model; disabling a model (`enabled: false`) removes its RayService **without touching any other model** — that's the per-model isolation doing its job.

After enabling, you still need to wire a gateway route so traffic can reach it — see [Adding a model](../operations/adding-a-model.md) for the full runbook.

## Where the weights come from: `model_source`

By default, the model id under `vllmArgs.model` is pulled from the Hugging Face Hub at first load. For production you'll usually pre-stage weights in your own bucket and point `model_source` at them:

```yaml
vllmArgs:
  model: "Qwen/Qwen3-8B"
  served_model_name: "Qwen/Qwen3-8B"
  model_source: "gs://<your-model-bucket>/public-models/Qwen/Qwen3-8B"
```

Supported source shapes:

| Source | Meaning |
|---|---|
| `hf://...` (or a bare HF id) | Pull from the Hugging Face Hub |
| `gs://bucket/path` | Google Cloud Storage — the worker ServiceAccount (`open-serve-worker`) needs read access, e.g. via Workload Identity |
| `s3://bucket/path` | S3-compatible object storage (IRSA or equivalent for credentials) |
| Local path | Weights already present in the image or on a mounted volume |

Pre-staged buckets avoid Hub rate limits and make cold starts a pure download-bandwidth problem inside your cloud.

## Contributing a preset

Adding a preset for a newly released open-weights model is the easiest way to contribute to open-serve. A good preset PR includes tested `vllmArgs`, at least one validated GPU sizing, and the probe metadata (`tier`, `runner`, `category`, `description`). See `CONTRIBUTING.md` in the repository root.
