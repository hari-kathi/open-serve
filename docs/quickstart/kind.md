# Local demo on kind

Run the whole open-serve stack on your laptop — no GPUs, no cloud account. The kind demo deploys a tiny CPU-only model behind the gateway so you can exercise the real request path: API-key auth → routing → a per-model RayService.

!!! note
    This is the **validated** quickstart: the kind demo is exercised locally as part of the release process. It is a demo, not a production layout — for production use the [GCP quickstart](gcp.md) and the FluxCD reference in `deploy/flux/`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (the kind nodes run as containers)
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) v3

Expect the demo to want several CPUs and ~8 GB of RAM available to Docker — Ray head pods plus a small model add up even on CPU.

## Run it

Everything lives in `examples/kind-local/`:

```bash
cd examples/kind-local
./run.sh
```

The script drives the full setup end to end. At a high level it:

1. Creates a kind cluster from the bundled `kind.yaml`.
2. Installs the KubeRay operator (open-serve models are RayService CRDs — the operator reconciles them).
3. Installs the `charts/open-serve` Helm chart with the bundled `values.yaml`: a small CPU-only model under `serveModels`, plus the gateway enabled in front of it.
4. Creates the `open-serve-api-keys` Secret with a demo API key so the gateway has something to authenticate against.
5. Waits for the RayService to come up and prints the port-forward / curl instructions.

Check the script output for the exact demo model name and API key it configures — the examples below use placeholders.

## Poke at it

Port-forward the gateway (if `run.sh` hasn't already told you to):

```bash
kubectl -n open-serve port-forward svc/open-serve-gateway 8000:8000
```

**No key → 401.** The gateway rejects requests without a valid Bearer token:

```bash
curl -i http://localhost:8000/v1/models
# HTTP/1.1 401 Unauthorized
# {"detail":"Missing Bearer token"}
```

**With the demo key → chat completion:**

```bash
export API_KEY=sk-demo-...   # printed by run.sh

curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<demo-model>",
    "messages": [{"role": "user", "content": "Say hello in five words."}],
    "max_tokens": 32
  }'
```

**List models** (the gateway aggregates `/v1/models` across every backend it knows about):

```bash
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer $API_KEY"
```

You can also run the full smoke suite against the local gateway:

```bash
BASE_URL=http://localhost:8000 API_KEY=$API_KEY \
  CHAT_MODELS="<demo-model>" ./scripts/test-endpoints.sh
```

## What you just deployed

| Component | What it is |
|---|---|
| KubeRay operator | Reconciles RayService CRDs into Ray clusters |
| `rayservice-<model>` | The demo model: its own Ray head + worker pods, serving an OpenAI-compatible app on port 8000 |
| `open-serve-gateway` | Auth (Bearer key → source), routing, and `openserve_requests_total` usage metrics |
| `open-serve-api-keys` Secret | The key map the gateway validates against ([details](../operations/api-keys.md)) |

The first chat request may be slow: on CPU, model load and cold start take a while. Watch progress with `kubectl -n open-serve get pods -w`.

## Cleanup

```bash
cd examples/kind-local
./cleanup.sh
```

This tears down the kind cluster (and with it everything deployed into it).

## Next steps

- [End-to-end on GCP](gcp.md) — the same stack on real GPUs.
- [Adding a model](../operations/adding-a-model.md) — swap the demo model for one from the catalog.
- [Architecture](../concepts/architecture.md) — what the pieces are and why each model gets its own RayService.
