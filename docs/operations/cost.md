# Cost

GPU serving is expensive, and most of the cost story is "don't run GPUs you aren't using." This page is explicit about what open-serve gives you **today** versus what is **planned**.

## What exists today

### Scale-to-zero

Each model sets its own Serve autoscaling bounds. With `replicas.min: 0`, an idle model's replicas scale away after `downscaleDelayS` of inactivity; with worker bounds following suit, the GPU pods — and, via the cluster autoscaler, the GPU **nodes** — go away entirely. An idle model costs nothing but its (CPU-only) Ray head pod.

```yaml
serveModels:
  qwen3-8b:
    replicas: { min: 0, max: 1 }
    autoscaling:
      downscaleDelayS: 600    # 10 min idle → scale down
      upscaleDelayS: 60
```

The trade-off is cold-start latency on the first request (node provision + image pull + weight load). Typical policy: `tier: internal-test` models scale to zero; `tier: production` models keep `min: 1` and pay for warmth.

### Autoscaling caps

`replicas.max` and `replicas.workerMax` are hard spend ceilings per model — a traffic spike (or a runaway client) can never provision more than `workerMax` GPU nodes for that model. Because every model has its own RayService, caps are genuinely per model: one model's burst can't starve or inflate another's pool.

Related levers for squeezing utilization:

- **Fractional GPUs** — `gpu.count: 0.25` packs four Serve replicas of a small model onto one physical GPU (`gpu.workerGpus` controls what the pod requests). Profile VRAM first: each replica typically loads its own copy of the weights.
- **Right-sized accelerators** — `gpu.acceleratorType` + `nodeSelector` route each model to the cheapest SKU that fits it, rather than defaulting everything to the biggest card.

!!! note
    If you change `replicas.max` on a live model and it doesn't seem to take effect, see `rayClusterChecksum.enabled` in values.yaml — a kuberay-operator gap means min/max changes don't propagate to the active RayCluster without a cluster swap.

### Spot / preemptible pools (Terraform reference)

The GCP reference infrastructure (`terraform/gcp/`, surfaced via `examples/gcp-quickstart/`) is being built with scale-to-zero GPU node pools, multi-SKU tiering, and **spot pool support** so interruption-tolerant workloads (internal-test models, batch-ish traffic) run at spot prices. Status: the pool-map refactor, spot support, and budget alerts are in progress — check `terraform/README.md` for the current state. Nothing in the chart assumes spot; it's an infrastructure-layer choice.

### Attribution signals you already have

Even without a cost dashboard, the raw attribution data exists today:

- `openserve_requests_total{source, model, org, tier}` and `openserve_tokens_total{source, model, org, token_type, tier}` from the gateway — per-consumer request and token volumes.
- Ray/GPU utilization metrics per model (pods are labeled `model=<name>`), so "GPU-hours per model" is derivable with PromQL.

## Roadmap (not built yet)

Planned cost features — none of these ship in the current chart:

- **Pricing-map recording rules** — a per-SKU $/GPU-hour map rendered into Prometheus recording rules, turning GPU-seconds per model into dollars per model directly in PromQL.
- **Cost dashboard** — a Grafana dashboard showing **$/model** (GPU-hour attribution) and **$/1M tokens** (joining the pricing map with `openserve_tokens_total`), per source/org.
- **OpenCost integration** — reconciling the model-level attribution with cluster-level cost allocation (shared nodes, non-GPU overhead) via [OpenCost](https://www.opencost.io/), rather than reinventing node pricing.

Until then, the practical approach is: cap spend with `replicas.max`, scale to zero what you can, and join `openserve_tokens_total` with your cloud bill by hand.
