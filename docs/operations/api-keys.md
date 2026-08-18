# API keys

The gateway authenticates every non-public request with a Bearer token and resolves it to a **source** — a short name for the team, app, or system that owns the key. The source becomes a metric label, so usage and errors are attributable per consumer without any per-request configuration.

## The key map

Keys live in a single Kubernetes Secret, read by the gateway at startup:

- **Secret name:** `open-serve-api-keys` (configurable via `gateway.apiKeySecret.name`)
- **Key:** `key-map.json` (configurable via `gateway.apiKeySecret.key`)
- **Format:** a JSON object mapping *key value → source name*:

```json
{
  "sk-team1-4f9a...e2": "team1",
  "sk-app2-b81c...77": "app2",
  "sk-monitor-09df...3a": "monitor"
}
```

The gateway treats the key as an opaque string — any value works — but the convention is `sk-<source>-<hex>`, which makes a leaked key self-identifying and greppable. Generate the hex part with something like:

```bash
echo "sk-team1-$(openssl rand -hex 16)"
```

The Secret is deliberately **not** part of the GitOps repo. Create and manage it out of band (kubectl, your secret manager's Kubernetes integration, sealed-secrets — whatever your cluster uses).

## Creating the Secret

```bash
kubectl -n open-serve create secret generic open-serve-api-keys \
  --from-literal=key-map.json='{
    "sk-team1-'"$(openssl rand -hex 16)"'": "team1",
    "sk-monitor-'"$(openssl rand -hex 16)"'": "monitor"
  }'
```

Or from a file:

```bash
kubectl -n open-serve create secret generic open-serve-api-keys \
  --from-file=key-map.json=./key-map.json
```

The gateway mounts the Secret at `/secrets/key-map.json` (env var `API_KEY_MAP_FILE`) and loads it **once at startup**.

## Adding and rotating keys

To add a source or rotate a key:

1. Update the JSON (add the new key; for rotation, add the new key *alongside* the old one first).
2. Replace the Secret:
   ```bash
   kubectl -n open-serve create secret generic open-serve-api-keys \
     --from-file=key-map.json=./key-map.json \
     --dry-run=client -o yaml | kubectl apply -f -
   ```
3. Restart the gateway so it re-reads the map (the key map is loaded at process start; a Secret update alone does not take effect):
   ```bash
   kubectl -n open-serve rollout restart deployment/open-serve-gateway
   ```
   The gateway runs 2 replicas with a PodDisruptionBudget, so the rollout is hitless.
4. For rotation: once the consumer has switched to the new key, remove the old entry and repeat steps 2–3.

Because both keys can coexist in the map (mapping to the same source), rotation needs no downtime window.

## The `source` label in metrics

Every authenticated request increments:

```
openserve_requests_total{source="team1", model="Qwen/Qwen3-8B", org="...", tier="production"}
```

- `source` — resolved from the key map. Failed auth records `source="unknown"` with `error_type="auth_failed"` in `openserve_errors_total`.
- `org` — read from the `x-openserve-org-id` header (name configurable via `ORG_ID_HEADER`), for callers that multiplex several downstream orgs over one key.
- Token usage lands in `openserve_tokens_total{source, model, org, token_type, tier}` (prompt/completion), extracted from non-streaming responses.

This makes "who is using which model how much" a straight PromQL query, e.g. `sum by (source, model) (rate(openserve_requests_total[1h]))`.

## The probe's key: `authSourceName`

The synthetic probe exercises the **external** path through the gateway, so it needs a real API key. Rather than configuring the key twice, the probe mounts the same `open-serve-api-keys` Secret and selects the key whose *source* equals `probe.authSourceName` — by convention **`monitor`**:

```yaml
probe:
  enabled: true
  authSourceName: "monitor"
```

So: keep one entry in the key map whose source is `monitor` (e.g. `"sk-monitor-<hex>": "monitor"`). If no key with that source exists, the probe logs a warning and runs external probes unauthenticated — which then fail with 401. A useful side effect of the convention: probe traffic shows up in usage metrics as `source="monitor"`, cleanly separable from real consumers.

## What doesn't need a key

The gateway's own endpoints (`/`, `/health`, `/healthz`, `/metrics`) are served locally without auth, and the status-page path tree (`/status`, `/status.json`, `/static/` — configurable via `PUBLIC_FORWARD_PREFIXES`) is forwarded without auth, metered as `source="public"`. Everything else requires a valid Bearer token. See the [API reference](../reference/api.md#authentication).
