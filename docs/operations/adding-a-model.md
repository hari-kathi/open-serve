# Adding a model

The runbook for putting a new model behind the gateway, from preset to validated endpoint.

**Overview:** pick a runner → add a `serveModels` entry → wire a gateway route → deploy → validate.

## 1. Pick a runner

See [Runners](../concepts/runners.md) for the full decision table. Short version:

- Any OpenAI-compatible model — chat, completions, embeddings (via pooling), multimodal → `vllm`
- LLM where the chat/completions/embeddings subset is enough and you prefer `LLMConfig`-style config → `ray-serve-llm`
- Anything else (reranker, OCR, CNN) → `custom`

## 2. Add or merge a `serveModels` entry

Start from a catalog preset if one exists (`catalog/models/` — see [Model catalog](../concepts/model-catalog.md)); otherwise copy the commented skeleton in `charts/open-serve/values.yaml`. Merge it into your environment values (for Flux: your overlay's `open-serve/values.yaml`) and set `enabled: true`.

Fields that matter operationally:

- `modelId` — what clients pass as `"model"` and what the probe asserts appears in `/v1/models`.
- `tier` — `internal-test` (default) is never probed and may scale to zero freely; promote to `production` once the model is customer-facing (this enables probing and tier-gated alerts).
- `runner` — `chat` or `embedding` get functional probes; other values get generic liveness only.
- `replicas` / `autoscaling` / `gpu` / `resources` — capacity. Keep `replicas: {min: 0, max: 1}` while validating.

## 3. Wire the gateway route

One instruction: add the served model id to `gateway.modelRoutes`. The gateway routes every authenticated request — chat, completions, embeddings, `/tokenize`, `/detokenize`, `/v1/responses`, everything — by the request body's `model` field. Each model's Service is `rayservice-<name>-serve-svc` on port 8000:

```yaml
gateway:
  modelRoutes:
    "Qwen/Qwen3-8B":              # must match what clients send as "model"
      service: "rayservice-qwen3-8b-serve-svc"
      port: 8000
```

Embedding models are wired identically — embedding requests carry a `model` field like everything else. A request whose body has no `model` field gets a `400`; a `model` value not present in `modelRoutes` gets a `404`.

## 4. Deploy

- **Flux**: commit and merge to the tracked branch; Flux upgrades the HelmRelease.
- **Plain Helm**: `helm upgrade open-serve charts/open-serve -n open-serve -f your-values.yaml`

!!! note "Helm v4 server-side apply conflicts with the KubeRay operator"
    Once a RayService is live, the KubeRay operator takes field ownership of
    `spec.rayClusterConfig.workerGroupSpecs` (it writes autoscaled replica
    counts back). Helm v4 upgrades use server-side apply and will fail with a
    field-manager conflict on existing RayServices — pass `--force-conflicts`
    (your values remain the source of truth; the operator re-adopts replicas
    afterwards). Helm v3's client-side strategy and Flux's SSA (which forces
    by default) are unaffected.

Watch it come up:

```bash
kubectl -n open-serve get rayservice rayservice-qwen3-8b -w
kubectl -n open-serve get pods -l model=qwen3-8b
```

First start on a fresh GPU node includes node provisioning, image pull, and weight load — be patient. If a worker pod sticks in `Pending`, check `kubectl get events` for GPU quota / stockout (this is what the `OpenServeWorkerPending` alert catches).

!!! note "Serve-script changes need a pod restart"
    Helm updates the `serve-<model>-script` ConfigMaps, but running Ray pods keep the files they mounted at startup. After changing a serve script or engine params, either delete the model's head+worker pods, or enable `rayClusterChecksum.enabled: true` so any per-model change triggers a KubeRay zero-downtime swap automatically.

## 5. Validate

```bash
kubectl -n open-serve port-forward svc/open-serve-gateway 8000:8000

BASE_URL=http://localhost:8000 API_KEY=<your-key> \
  CHAT_MODELS="Qwen/Qwen3-8B" ./scripts/test-endpoints.sh
```

`test-endpoints.sh` smoke-tests the whole gateway surface: models list, chat (plain/streaming/tools), embeddings (`EMBED_MODELS=...`), `/v1/responses`, tokenize/detokenize roundtrip, auth enforcement, and error handling. For an interactive session, `scripts/connect.sh` port-forwards the gateway, Ray dashboard, Grafana, and Prometheus, and lets you chat with the model.

## Worked example: Qwen3-8B from the catalog

Environment values, end to end (preset from `catalog/models/qwen3-8b.yaml`):

```yaml
gateway:
  enabled: true
  modelRoutes:
    "Qwen/Qwen3-8B":
      service: "rayservice-qwen3-8b-serve-svc"
      port: 8000

serveModels:
  qwen3-8b:
    enabled: true
    modelId: "Qwen/Qwen3-8B"
    tier: internal-test              # promote to production after validation
    runner: chat
    category: LLM
    description: "Qwen3 8B general-purpose chat model"
    type: vllm
    rayVersion: "2.55.1"
    image:
      repository: "hari-kathi/open-serve-vllm"
      tag: "{{ release_version_bare }}"
    routePrefix: "/"
    sharedMemorySize: "20Gi"
    vllmArgs:
      model: "Qwen/Qwen3-8B"
      served_model_name: "Qwen/Qwen3-8B"
      tensor_parallel_size: 1
      dtype: "bfloat16"
      max_model_len: 32768
      max_num_seqs: 64
      gpu_memory_utilization: 0.90
      enable_auto_tool_choice: true
      tool_call_parser: "hermes"
    replicas: { min: 0, max: 1 }
    autoscaling:
      targetOngoingRequests: 5
      downscaleDelayS: 600
      upscaleDelayS: 60
    gpu:
      count: 1
      acceleratorType: "A100"
    resources:
      worker:
        requests: { cpu: "8", memory: "32Gi" }
        limits: { cpu: "8", memory: "48Gi" }
```

Deploy, then:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen3-8B", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32}'
```

## 6. Promote to production

Once validated:

1. Set `tier: production` (the probe starts covering it; availability alerts start gating on it).
2. Consider `replicas.min: 1` so users and probes hit warm replicas.
3. Add a latency SLO under `monitoring.alerts.probes.slo.perModel` (e.g. `qwen3-8b: 10`) — this both renders a `ModelProbeHighLatency` alert and feeds the status page's degraded classification. See [Observability](observability.md).
