#!/usr/bin/env python3
"""
Measure Ray Serve scale-up time for any model registered in the live chart.

The script drives concurrent traffic above the autoscaler's per-replica
threshold, watches a new worker pod come up, and records each kubelet/Ray
condition transition. Output is a phase-by-phase breakdown plus a JSON
report — diagnostic / measurement only.

Real-time alerting is handled by cluster-side PrometheusRules so coverage
extends to organic customer traffic, not just synthetic test runs. The
script never posts alerts anywhere; it just prints, optionally writes JSON,
and exits non-zero on failure or threshold breach for CI integration.

Auto-discovery
==============

Given `--model <helm-key>`, the script reads the live HelmRelease values
ConfigMap (`open-serve` release in the `open-serve` namespace) and derives:

  * `runner`       — chooses endpoint (`chat` → /v1/chat/completions,
                     `embedding` → /v1/embeddings). Defaults to `chat`
                     when unset, matching the chart default.
  * `modelId`      — the API id sent in the request body. Falls back to
                     the Helm key when unset. For mxbai-embed-large-v1 this
                     resolves to `mixedbread-ai/mxbai-embed-large-v1`.
  * `autoscaling.targetOngoingRequests` — used to size concurrency so
                     ongoing-requests reliably exceeds the autoscaler's
                     threshold. Empirically for an embedding model with
                     `target_ongoing_requests=10` and p95 latency 40ms,
                     concurrency=60 wasn't enough — fast requests barely
                     register as in-flight. The auto-formula gives plenty
                     of headroom over the autoscaler's signal.

`--endpoint`, `--api-model`, `--concurrency` can override any auto-detected
value for one-off experiments.

Phases captured (per new worker pod)
====================================

    T0  load_start          — script begins sending concurrent traffic
    T1  pod_created          — new worker pod's metadata.creationTimestamp
                                (Ray Serve autoscaler decision → KubeRay
                                creates the pod)
    T2  pod_scheduled        — PodScheduled condition lastTransitionTime
                                (cluster-autoscaler may need to provision
                                a fresh GPU node before this happens)
    T3  pod_initialized      — Initialized condition (init containers done,
                                images pulled)
    T4  containers_ready     — ContainersReady condition (ray-worker
                                container reports running, but Ray's
                                readinessProbe — which gates on per-replica
                                RUNNING state in the dashboard — has not
                                yet flipped to ready)
    T5  pod_ready            — Ready condition (workerReadinessProbe passes
                                → THIS pod hosts a replica in RUNNING state
                                in Ray Serve → replica is serving traffic)

Phase deltas:

    decision     T0 → T1   autoscaler latency (governed by
                            upscale_delay_s in deployment_config)
    schedule     T1 → T2   kube-scheduler + cluster-autoscaler
                            (cold node = ~60–180s, warm = <5s)
    init         T2 → T3   image pull + initContainers
    container    T3 → T4   container start
    model_load   T4 → T5   GCS download + weight load + torch.compile +
                            CUDA graph capture (the dominant phase for
                            large models — typically several minutes)

Total: T0 → T5

Early-failure detection
=======================

30 seconds after starting traffic, the script queries Prometheus for the
Ray Serve request counter for the target model and compares the delta
against `gen.success` (script-side HTTP 200s). If less than 10% of the
script's requests reached Ray Serve, the script exits non-zero with a
diagnostic — covering the failure modes seen in practice:
  * wrong API model id (auth proxy returns 404 from Ray Serve)
  * wrong endpoint for the runner (auth proxy returns 404 or 405)
  * auth proxy short-circuiting requests for some other reason

Examples
========

    # List every registered model (no traffic sent):
    ./scripts/test-scaleup.py --list-models

    # Test any model. The script picks the right endpoint, api id,
    # and concurrency from the live chart values:
    ./scripts/test-scaleup.py --model qwen3-8b
    ./scripts/test-scaleup.py --model mxbai-embed-large-v1

    # CI-friendly: structured output, exit non-zero on threshold breach:
    ./scripts/test-scaleup.py \\
        --model qwen3-8b \\
        --threshold-seconds 360 \\
        --json-out /tmp/scaleup-report.json

    # Override any auto-detected setting for a one-off experiment:
    ./scripts/test-scaleup.py \\
        --model mxbai-embed-large-v1 \\
        --concurrency 200 \\
        --endpoint embeddings
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:
    sys.stderr.write(
        "test-scaleup.py requires PyYAML to parse the live HelmRelease values.\n"
        "Install with: pip install PyYAML\n"
    )
    raise


# ---------- terminal colors -------------------------------------------------

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_COLOR = _color_enabled()
_C = {
    "reset": "\033[0m" if _COLOR else "",
    "bold": "\033[1m" if _COLOR else "",
    "dim": "\033[2m" if _COLOR else "",
    "red": "\033[31m" if _COLOR else "",
    "green": "\033[32m" if _COLOR else "",
    "yellow": "\033[33m" if _COLOR else "",
    "blue": "\033[34m" if _COLOR else "",
    "cyan": "\033[36m" if _COLOR else "",
}


def log(msg: str, level: str = "info") -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S")
    color = {"info": "cyan", "ok": "green", "warn": "yellow", "err": "red"}.get(level, "cyan")
    print(f"{_C['dim']}[{ts}]{_C['reset']} {_C[color]}{msg}{_C['reset']}", flush=True)


# ---------- kubectl helpers -------------------------------------------------

# Namespace the chart is deployed into. Override with the NAMESPACE env var.
DEFAULT_NAMESPACE = os.environ.get("NAMESPACE", "open-serve")


def kubectl(args: list[str], context: str, namespace: Optional[str] = None) -> str:
    if namespace is None:
        namespace = DEFAULT_NAMESPACE
    cmd = ["kubectl", "--context", context]
    if namespace != "":
        cmd += ["-n", namespace]
    cmd += args
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def list_worker_pods(context: str, helm_key: str) -> list[dict]:
    """Return current worker pods for a model (parsed kubectl JSON).

    The selector uses `model=<helm-key>` — chart-applied label from
    PodMonitor relabels. NOT the API model id (which may differ from
    the helm key for some models like mxbai)."""
    out = kubectl(
        [
            "get", "pods",
            "-l", f"ray.io/node-type=worker,model={helm_key}",
            "-o", "json",
        ],
        context=context,
    )
    return json.loads(out).get("items", [])


def get_pod(context: str, name: str) -> Optional[dict]:
    try:
        return json.loads(kubectl(["get", "pod", name, "-o", "json"], context=context))
    except subprocess.CalledProcessError:
        return None


def get_pod_failure_events(context: str, name: str) -> list[dict]:
    """Return any FailedScheduling / FailedScaleUp / ScaleUpFailed events
    targeting this pod. Used to surface autoscaler / scheduler failures
    (e.g. GCE STOCKOUT) explicitly rather than swallowing them as a timeout."""
    try:
        out = kubectl(
            ["get", "events", "--field-selector",
             f"involvedObject.kind=Pod,involvedObject.name={name}",
             "-o", "json"],
            context=context,
        )
        items = json.loads(out).get("items", [])
    except subprocess.CalledProcessError:
        return []
    return [e for e in items
            if e.get("reason") in
            ("FailedScheduling", "FailedScaleUp", "ScaleUpFailed",
             "NotTriggerScaleUp", "TriggeredScaleUp")]


def summarize_failure_events(events: list[dict]) -> str:
    """Compact, human-readable summary of pod-level autoscaler events
    suitable for terminal output."""
    lines = []
    for e in events:
        reason = e.get("reason", "?")
        msg = e.get("message", "").strip().replace("\n", " ")
        if len(msg) > 240:
            msg = msg[:237] + "..."
        lines.append(f"  {reason}: {msg}")
    return "\n".join(lines) if lines else "(no events)"


# ---------- model config auto-discovery ------------------------------------

# runner → endpoint convention. Matches the chart's `runner:` field and
# the auth proxy's path-prefix routing.
RUNNER_TO_ENDPOINT = {
    "chat": "chat",
    "embedding": "embeddings",
}


@dataclass
class ModelConfig:
    helm_key: str
    api_id: str
    runner: str
    endpoint: str   # "chat" or "embeddings"
    target_ongoing_requests: int
    min_replicas: int
    max_replicas: int
    tier: str
    enabled: bool


def discover_models(context: str) -> dict[str, ModelConfig]:
    """Read the live HelmRelease values ConfigMap and return one
    ModelConfig per entry in `serveModels`.

    Falls back gracefully when individual fields are missing (e.g.
    `runner` defaults to `chat`, `modelId` defaults to the helm key)."""
    # Find the values ConfigMap that ray-serve HelmRelease points at
    hr_json = kubectl(
        ["get", "hr", "open-serve", "-o", "json"],
        context=context,
    )
    hr = json.loads(hr_json)
    values_from = hr.get("spec", {}).get("valuesFrom", [])
    cm_name = next(
        (v["name"] for v in values_from if v.get("kind") == "ConfigMap"),
        None,
    )
    if not cm_name:
        raise RuntimeError(
            "open-serve HelmRelease has no ConfigMap in spec.valuesFrom; "
            "cannot auto-discover models"
        )
    cm_json = kubectl(["get", "cm", cm_name, "-o", "json"], context=context)
    cm = json.loads(cm_json)
    values_yaml = cm.get("data", {}).get("values.yaml", "")
    values = yaml.safe_load(values_yaml) or {}

    out: dict[str, ModelConfig] = {}
    for helm_key, m in (values.get("serveModels") or {}).items():
        if not isinstance(m, dict):
            continue
        runner = (m.get("runner") or "chat").lower()
        endpoint = RUNNER_TO_ENDPOINT.get(runner)
        if endpoint is None:
            # unknown runner — skip silently; --model with this key will
            # still produce a clear "unsupported runner" error if invoked
            continue
        api_id = m.get("modelId") or helm_key
        autoscaling = m.get("autoscaling") or {}
        replicas = m.get("replicas") or {}
        out[helm_key] = ModelConfig(
            helm_key=helm_key,
            api_id=api_id,
            runner=runner,
            endpoint=endpoint,
            target_ongoing_requests=int(autoscaling.get("targetOngoingRequests", 5)),
            min_replicas=int(replicas.get("min", 1)),
            max_replicas=int(replicas.get("max", 2)),
            tier=(m.get("tier") or "internal-test"),
            enabled=bool(m.get("enabled", False)),
        )
    return out


def format_model_catalog(models: dict[str, ModelConfig]) -> str:
    """Pretty-print the discovered models for --list-models or error output."""
    if not models:
        return "  (no models found — is the open-serve HelmRelease deployed?)"
    rows = [("helm_key", "runner", "api_id", "target_ongoing", "min/max", "tier", "enabled")]
    for m in sorted(models.values(), key=lambda x: x.helm_key):
        rows.append((
            m.helm_key,
            m.runner,
            m.api_id,
            str(m.target_ongoing_requests),
            f"{m.min_replicas}/{m.max_replicas}",
            m.tier,
            "yes" if m.enabled else "no",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join(fmt.format(*r) for r in rows)


def auto_concurrency(cfg: ModelConfig) -> int:
    """Pick a concurrency that reliably pushes ongoing-requests past the
    autoscaler's target. For fast endpoints (embeddings, p95 ~40ms) the
    in-flight count rarely matches concurrency, so we over-provision."""
    base = cfg.target_ongoing_requests * cfg.min_replicas
    # 4x headroom — empirically embeddings need this because each request
    # spends only ~40ms in-flight while the script's thread spends much
    # longer in HTTP/TCP/TLS overhead.
    return max(int(4 * base + 5), 12)


# ---------- timing model ----------------------------------------------------

@dataclass
class PodPhase:
    name: str
    timestamps: dict[str, Optional[str]] = field(default_factory=dict)
    node: Optional[str] = None

    def set_ts(self, key: str, value: Optional[str]) -> None:
        if value and not self.timestamps.get(key):
            self.timestamps[key] = value

    def parse(self, ts: Optional[str]) -> Optional[float]:
        if not ts:
            return None
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        ).timestamp()

    def epochs(self) -> dict[str, Optional[float]]:
        return {k: self.parse(v) for k, v in self.timestamps.items()}


def update_pod_phase(phase: PodPhase, pod: dict) -> bool:
    """Read the latest pod object and stamp condition transitions on phase.

    Returns True once the pod is Ready (the terminal condition we wait for).
    """
    meta = pod.get("metadata", {})
    spec = pod.get("spec", {})
    status = pod.get("status", {})
    phase.set_ts("pod_created", meta.get("creationTimestamp"))
    phase.node = spec.get("nodeName") or phase.node

    cond_map = {c["type"]: c for c in status.get("conditions", [])}
    for cond_type, key in [
        ("PodScheduled", "pod_scheduled"),
        ("Initialized", "pod_initialized"),
        ("ContainersReady", "containers_ready"),
        ("Ready", "pod_ready"),
    ]:
        c = cond_map.get(cond_type, {})
        if c.get("status") == "True":
            phase.set_ts(key, c.get("lastTransitionTime"))

    return cond_map.get("Ready", {}).get("status") == "True"


# ---------- traffic generator -----------------------------------------------

PROMPTS = [
    "Summarize the key principles of distributed systems in three sentences.",
    "Explain how transformer attention works.",
    "Write a haiku about cluster autoscaling.",
    "List three benefits of GPU memory pooling.",
    "Describe the CAP theorem.",
]

# endpoint → (url path, payload builder). Extend by adding to either dict;
# anything else surfaces a clear error at startup.
_ENDPOINT_PATHS = {
    "chat": "/v1/chat/completions",
    "embeddings": "/v1/embeddings",
}


def _build_payload(endpoint: str, api_id: str, max_tokens: int) -> bytes:
    prompt = random.choice(PROMPTS)
    if endpoint == "embeddings":
        return json.dumps({"model": api_id, "input": prompt}).encode("utf-8")
    if endpoint == "chat":
        return json.dumps({
            "model": api_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }).encode("utf-8")
    raise ValueError(f"unsupported endpoint {endpoint!r}")


class TrafficGenerator:
    def __init__(
        self,
        url: str,
        api_key: str,
        api_id: str,
        endpoint: str,
        concurrency: int,
        max_tokens: int = 64,
    ) -> None:
        if endpoint not in _ENDPOINT_PATHS:
            raise ValueError(
                f"unsupported endpoint {endpoint!r}; "
                f"known: {sorted(_ENDPOINT_PATHS)}"
            )
        self.endpoint = endpoint
        self.api_id = api_id
        self.url = url.rstrip("/") + _ENDPOINT_PATHS[endpoint]
        self.api_key = api_key
        self.concurrency = concurrency
        self.max_tokens = max_tokens
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.success = 0
        self.errors = 0
        # Track distinct error categories so the operator can tell auth
        # failures from network timeouts from payload rejections.
        self.status_counts: dict[int, int] = {}
        self.lock = threading.Lock()

    def _worker(self, worker_id: int) -> None:
        while not self.stop.is_set():
            payload = _build_payload(self.endpoint, self.api_id, self.max_tokens)
            req = urllib.request.Request(
                self.url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            status = 0
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                    status = r.status
                with self.lock:
                    self.success += 1
                    self.status_counts[status] = self.status_counts.get(status, 0) + 1
            except urllib.error.HTTPError as e:
                with self.lock:
                    self.errors += 1
                    self.status_counts[e.code] = self.status_counts.get(e.code, 0) + 1
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                with self.lock:
                    self.errors += 1
            time.sleep(random.uniform(0.05, 0.25))

    def start(self) -> None:
        for i in range(self.concurrency):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)


# ---------- Prometheus reach-check -----------------------------------------

# When we send N requests, at least this fraction should reach Ray Serve
# for the test to be considered meaningful. Below this, something is
# dropping traffic before it gets to the model (auth proxy 404, wrong
# api id, wrong endpoint, etc.).
REACH_CHECK_MIN_RATIO = 0.10
# Wait two full Prometheus scrape cycles (30s each) after starting traffic
# before reading the counter. At T+30s the counter often shows almost no
# delta because Prometheus' last scrape may have happened *before* the
# script started sending traffic — yielding a false-positive reach-check
# failure. 60s gives at least one post-traffic scrape, usually two.
REACH_CHECK_AFTER_SEC = 60


def _prom_query(prom_url: str, query: str) -> Optional[float]:
    """Single-value scalar query against Prometheus HTTP API. Returns None
    on any failure (network, parse, no series)."""
    try:
        url = prom_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
    except Exception:
        return None
    results = (data.get("data") or {}).get("result") or []
    if not results:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


def _ray_serve_request_counter(prom_url: str, helm_key: str, endpoint: str) -> Optional[float]:
    """Sum of ray_serve request counters at the OpenAiIngress deployment
    for this model and endpoint. Returns None if Prometheus is unreachable
    or the series doesn't exist (model hasn't received any requests ever)."""
    route = _ENDPOINT_PATHS.get(endpoint, "")
    # Match by serve application name. The chart sets this to
    # `<helm-key>-serve` for vLLM models. Filter to the OpenAiIngress
    # deployment so we count requests once (Ray Serve emits one counter
    # for the ingress and another for each downstream replica).
    q = (
        'sum(ray_serve_deployment_request_counter_total{'
        f'application="{helm_key}-serve",'
        f'deployment="OpenAiIngress",'
        f'route="{route}"'
        '})'
    )
    return _prom_query(prom_url, q)


# ---------- main flow -------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Measure Ray Serve scale-up time and break it down by phase.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--model", default=None,
                   help="model helm key (used as the Kubernetes pod label "
                        "selector). The API id sent in request bodies comes "
                        "from the chart's `modelId` field for this entry, "
                        "or falls back to the helm key. Use --list-models to "
                        "see what's available.")
    p.add_argument("--api-model", default=None,
                   help="override the API `model` field in request bodies "
                        "(advanced; defaults to chart `modelId` or helm key).")
    p.add_argument(
        "--context",
        default=os.environ.get("KUBE_CONTEXT"),
        help="kubectl context (or KUBE_CONTEXT env var; defaults to the "
             "current kubeconfig context)",
    )
    p.add_argument(
        "--api-url",
        default=os.environ.get("BASE_URL"),
        help="gateway base URL (or BASE_URL env var; required), e.g. "
             "https://models.example.com or an internal LB address",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("API_KEY"),
        help="bearer token (or set API_KEY env var)",
    )
    p.add_argument(
        "--api-key-from-secret",
        action="store_true",
        help="read the 'monitor' key from the live open-serve-api-keys secret",
    )
    p.add_argument(
        "--concurrency", type=int, default=None,
        help="concurrent in-flight requests. Default is auto-computed as "
             "max(4 * target_ongoing_requests * min_replicas + 5, 12) "
             "from the live chart values.",
    )
    p.add_argument("--max-tokens", type=int, default=64,
                   help="per-request output tokens for chat endpoint (ignored for embeddings)")
    p.add_argument(
        "--endpoint", choices=sorted(_ENDPOINT_PATHS), default=None,
        help="override the API endpoint (advanced; defaults to chart `runner` mapping).",
    )
    p.add_argument(
        "--max-wait", type=int, default=900,
        help="max seconds to wait for new replica Ready (default 900 = 15 min)",
    )
    p.add_argument(
        "--threshold-seconds", type=float, default=None,
        help="exit code 2 if total scale-up exceeds this (e.g. 360 for 6-min SLO)",
    )
    p.add_argument(
        "--prometheus-url", default=None,
        help="Prometheus URL for the early-failure reach-check. Defaults to "
             "a local port-forward; set to a reachable URL to skip the "
             "implicit `kubectl port-forward` step.",
    )
    p.add_argument(
        "--skip-reach-check", action="store_true",
        help="disable the T+30s 'did traffic actually reach Ray Serve?' check.",
    )
    p.add_argument("--json-out", help="write structured report to this path")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and exit without sending traffic")
    p.add_argument("--list-models", action="store_true",
                   help="discover and print all registered models, then exit.")
    args = p.parse_args()

    # Default to the current kubeconfig context when none was given.
    if not args.context:
        try:
            args.context = subprocess.check_output(
                ["kubectl", "config", "current-context"], text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            log("--context (or KUBE_CONTEXT env var) required — no current "
                "kubeconfig context found", level="err")
            return 2

    if not args.api_url and not args.list_models and not args.dry_run:
        log("--api-url (or BASE_URL env var) required", level="err")
        return 2

    # --list-models: read the live chart and exit
    if args.list_models:
        try:
            models = discover_models(args.context)
        except Exception as e:
            log(f"failed to discover models: {e}", level="err")
            return 2
        print(f"{_C['bold']}Registered models on {args.context}:{_C['reset']}")
        print(format_model_catalog(models))
        return 0

    if not args.model:
        log("--model is required (or pass --list-models to see available)",
            level="err")
        try:
            models = discover_models(args.context)
            print()
            print(format_model_catalog(models), file=sys.stderr)
        except Exception:
            pass
        return 2

    # Auto-discover model config
    try:
        models = discover_models(args.context)
    except Exception as e:
        log(f"failed to discover models from cluster: {e}", level="err")
        return 2
    cfg = models.get(args.model)
    if cfg is None:
        log(f"model {args.model!r} not found in chart values. Available:",
            level="err")
        print(format_model_catalog(models), file=sys.stderr)
        return 2
    if not cfg.enabled:
        log(f"model {args.model!r} is disabled in chart values "
            f"(enabled=false)", level="err")
        return 2

    # Apply overrides
    endpoint = args.endpoint or cfg.endpoint
    api_id = args.api_model or cfg.api_id
    concurrency = args.concurrency or auto_concurrency(cfg)

    # Resolve API key
    if args.api_key_from_secret:
        try:
            data = kubectl(
                ["get", "secret", "open-serve-api-keys",
                 "-o", "jsonpath={.data.key-map\\.json}"],
                context=args.context,
            )
            key_map = json.loads(base64.b64decode(data))
            for k, v in key_map.items():
                if v == "monitor":
                    args.api_key = k
                    break
            else:
                log("no 'monitor' entry in open-serve-api-keys", level="err")
                return 2
        except subprocess.CalledProcessError as e:
            log(f"failed to read api key secret: {e.output}", level="err")
            return 2
    if not args.api_key and not args.dry_run:
        log("--api-key (or API_KEY env var, or --api-key-from-secret) required",
            level="err")
        return 2

    log(f"target model: {_C['bold']}{args.model}{_C['reset']} "
        f"(runner={cfg.runner}, api_id={api_id})",
        level="info")
    log(f"context:      {args.context}")
    log(f"api url:      {args.api_url}{_ENDPOINT_PATHS[endpoint]}")
    log(f"concurrency:  {concurrency}  "
        f"(auto from target_ongoing={cfg.target_ongoing_requests} × "
        f"min_replicas={cfg.min_replicas})"
        if args.concurrency is None
        else f"concurrency:  {concurrency}  (override)")
    log(f"max wait:     {args.max_wait}s")
    if args.threshold_seconds:
        log(f"threshold:    {args.threshold_seconds:.0f}s "
            f"(exit 2 if total > this)")

    # Snapshot baseline
    baseline = list_worker_pods(args.context, args.model)
    baseline_names = {p["metadata"]["name"] for p in baseline}
    log(f"baseline replicas: {len(baseline_names)}")
    for pod in baseline:
        log(f"  - {pod['metadata']['name']} on {pod['spec'].get('nodeName')}",
            level="info")

    if args.dry_run:
        log("dry run — exiting without sending traffic", level="ok")
        return 0

    # Optional Prometheus port-forward for the reach-check. Started in a
    # background subprocess and torn down at exit. Skipped if the user
    # passed --prometheus-url or --skip-reach-check.
    prom_url = args.prometheus_url
    prom_pf: Optional[subprocess.Popen] = None
    counter_at_start: Optional[float] = None
    if not args.skip_reach_check and not prom_url:
        local_port = 19090 + random.randint(0, 999)
        prom_url = f"http://127.0.0.1:{local_port}"
        prom_pf = subprocess.Popen(
            ["kubectl", "--context", args.context, "-n", DEFAULT_NAMESPACE,
             "port-forward", "svc/kube-prometheus-stack-prometheus",
             f"{local_port}:9090"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
    if not args.skip_reach_check:
        counter_at_start = _ray_serve_request_counter(prom_url, args.model, endpoint)
        if counter_at_start is None:
            log("Prometheus reach-check unavailable (counter series missing — "
                "model may not have served any requests yet). Will skip the "
                "T+30s reach-check.", level="warn")

    # Start load
    t0 = time.time()
    log(f"sending {concurrency} concurrent requests to {args.model}...",
        level="info")
    gen = TrafficGenerator(
        url=args.api_url,
        api_key=args.api_key,
        api_id=api_id,
        endpoint=endpoint,
        concurrency=concurrency,
        max_tokens=args.max_tokens,
    )
    gen.start()

    def _shutdown_all() -> None:
        gen.shutdown()
        if prom_pf and prom_pf.poll() is None:
            prom_pf.terminate()

    # Watch for new pod (with optional reach-check at T+30s)
    new_pod: Optional[str] = None
    deadline = t0 + args.max_wait
    last_replica_count = len(baseline_names)
    reach_checked = False
    while time.time() < deadline:
        time.sleep(5)
        pods = list_worker_pods(args.context, args.model)
        names = {p["metadata"]["name"] for p in pods}
        new_names = names - baseline_names
        if len(names) != last_replica_count:
            log(f"replica count: {last_replica_count} -> {len(names)} "
                f"(req success={gen.success} err={gen.errors})", level="info")
            last_replica_count = len(names)

        # Early-failure detection: are requests actually reaching Ray Serve?
        if (not reach_checked
                and not args.skip_reach_check
                and counter_at_start is not None
                and time.time() - t0 > REACH_CHECK_AFTER_SEC):
            counter_now = _ray_serve_request_counter(prom_url, args.model, endpoint)
            if counter_now is not None:
                delta = counter_now - counter_at_start
                ratio = delta / max(gen.success, 1)
                log(f"reach-check at T+{int(time.time() - t0)}s: "
                    f"script success={gen.success}, Ray Serve counter Δ={delta:.0f} "
                    f"(ratio {ratio:.1%}, status codes: {dict(gen.status_counts)})",
                    level="info")
                if ratio < REACH_CHECK_MIN_RATIO and gen.success > 50:
                    log(f"less than {REACH_CHECK_MIN_RATIO:.0%} of script traffic "
                        f"reached Ray Serve — load is being dropped upstream. "
                        f"Common causes: wrong api id (auth proxy → Ray Serve 404), "
                        f"wrong endpoint for runner ({cfg.runner}), "
                        f"or auth proxy short-circuit. Status codes seen: "
                        f"{dict(gen.status_counts)}",
                        level="err")
                    _shutdown_all()
                    return 1
            reach_checked = True

        if new_names:
            new_pod = sorted(new_names)[0]
            log(f"new pod detected: {_C['bold']}{new_pod}{_C['reset']}",
                level="ok")
            break
    else:
        _shutdown_all()
        log(f"no new replica appeared within {args.max_wait}s "
            f"(replicas stayed at {last_replica_count}, "
            f"req success={gen.success} err={gen.errors}, "
            f"status codes: {dict(gen.status_counts)})", level="err")
        # If the reach-check passed but no scale-up happened, the autoscaler
        # never saw enough ongoing-requests to fire. Most likely the per-
        # request latency is too low for `target_ongoing_requests` to ever
        # be reached at the script's concurrency. Suggest concrete fixes
        # instead of the generic "check quota" message.
        if reach_checked:
            log(f"reach-check passed earlier, so traffic IS hitting Ray Serve. "
                f"The autoscaler signal (ongoing requests per replica > "
                f"target_ongoing_requests={cfg.target_ongoing_requests}) "
                f"never tripped over {args.max_wait}s. For fast endpoints "
                f"(embeddings, rerank) each request stays in-flight only "
                f"~tens of ms, so ongoing-requests rarely reaches double "
                f"digits regardless of qps. Either: (a) increase "
                f"--concurrency further, (b) lower the model's "
                f"target_ongoing_requests in chart values, or "
                f"(c) switch the deployment_config to a qps-based "
                f"autoscaling metric.", level="warn")
        else:
            log("check autoscaler logs and GPU quota", level="warn")
        return 1

    # Track phase transitions, watching for autoscaler/scheduler failures
    phase = PodPhase(name=new_pod)
    autoscale_failure_logged = False
    while time.time() < deadline:
        pod = get_pod(args.context, new_pod)
        if pod is None:
            events = get_pod_failure_events(args.context, new_pod)
            elapsed = time.time() - t0
            log(f"new pod {new_pod} was deleted before reaching Ready "
                f"(elapsed {elapsed:.0f}s) — events:", level="err")
            log(summarize_failure_events(events), level="err")
            _shutdown_all()
            return 1

        ready = update_pod_phase(phase, pod)
        if phase.timestamps.get("pod_scheduled") and not phase.timestamps.get("_logged_scheduled"):
            log(f"  pod scheduled to node {phase.node}", level="ok")
            phase.timestamps["_logged_scheduled"] = "logged"

        if not phase.timestamps.get("pod_scheduled"):
            events = get_pod_failure_events(args.context, new_pod)
            hard_failures = [e for e in events
                             if e.get("reason") in
                             ("FailedScaleUp", "ScaleUpFailed")]
            if hard_failures and not autoscale_failure_logged:
                log("autoscaler failure(s) on new pod:", level="err")
                log(summarize_failure_events(hard_failures), level="err")
                autoscale_failure_logged = True
            stockout = any(
                "STOCKOUT" in e.get("message", "") or
                "RESOURCE_POOL_EXHAUSTED" in e.get("message", "")
                for e in hard_failures
            )
            if stockout:
                log("STOCKOUT — GCE has no capacity for the required GPU "
                    "in any configured zone. Add zones to the GPU node pool "
                    "or buy a reservation.", level="err")
                _shutdown_all()
                return 1

        if ready:
            log("  pod Ready (replica is RUNNING in Ray Serve)", level="ok")
            break
        time.sleep(5)
    else:
        _shutdown_all()
        events = get_pod_failure_events(args.context, new_pod)
        log(f"new pod {new_pod} did not reach Ready within {args.max_wait}s",
            level="err")
        if events:
            log(summarize_failure_events(events), level="err")
        return 1

    _shutdown_all()
    total = time.time() - t0

    # Compute deltas
    eps = phase.epochs()
    eps_t0 = t0

    def cum(key: str) -> Optional[float]:
        v = eps.get(key)
        return None if v is None else v - eps_t0

    rows = [
        ("T1 pod_created       (autoscaler decision → KubeRay)", cum("pod_created")),
        ("T2 pod_scheduled     (kube-scheduler ± autoscaler)",   cum("pod_scheduled")),
        ("T3 pod_initialized   (image pull + initContainers)",   cum("pod_initialized")),
        ("T4 containers_ready  (container started)",             cum("containers_ready")),
        ("T5 pod_ready         (replica RUNNING — model loaded)", cum("pod_ready")),
    ]

    def delta(a: str, b: str) -> Optional[float]:
        va, vb = eps.get(a), eps.get(b)
        return None if va is None or vb is None else vb - va

    deltas = [
        ("decision   ",     "T0 → T1", cum("pod_created")),
        ("schedule   ",     "T1 → T2", delta("pod_created", "pod_scheduled")),
        ("init       ",     "T2 → T3", delta("pod_scheduled", "pod_initialized")),
        ("container  ",     "T3 → T4", delta("pod_initialized", "containers_ready")),
        ("model_load ",     "T4 → T5", delta("containers_ready", "pod_ready")),
    ]

    # Print report
    print()
    print(f"{_C['bold']}=== Scale-up report: {args.model} on {args.context.split('_')[-1]} ==={_C['reset']}")
    print(f"runner:     {cfg.runner} (endpoint={endpoint}, api_id={api_id})")
    print(f"new pod:    {new_pod}")
    print(f"node:       {phase.node}")
    print(f"requests:   {gen.success} success / {gen.errors} errors  "
          f"(status: {dict(gen.status_counts)})")
    print(f"total:      {_C['bold']}{total:.1f}s{_C['reset']}")
    print()
    print(f"{_C['bold']}Cumulative offsets from T0 (load_start):{_C['reset']}")
    for label, val in rows:
        s = f"{val:.1f}s" if val is not None else "n/a"
        print(f"  {label:<55s} +{s}")
    print()
    print(f"{_C['bold']}Phase deltas:{_C['reset']}")
    for label, edge, val in deltas:
        if val is None:
            print(f"  {_C['dim']}{label} {edge}: n/a{_C['reset']}")
        else:
            color = "yellow" if val > 60 else "green"
            print(f"  {label} {edge}: {_C[color]}{val:.1f}s{_C['reset']}")
    print()

    # Threshold check (exit code only — alerting is cluster-side)
    breached = bool(args.threshold_seconds and total > args.threshold_seconds)
    if breached:
        log(f"total {total:.1f}s exceeded threshold {args.threshold_seconds:.0f}s",
            level="warn")

    # Structured output
    report = {
        "model": args.model,
        "api_id": api_id,
        "runner": cfg.runner,
        "endpoint": endpoint,
        "concurrency": concurrency,
        "context": args.context,
        "started_at": dt.datetime.fromtimestamp(t0, tz=dt.timezone.utc).isoformat(),
        "total_seconds": total,
        "threshold_seconds": args.threshold_seconds,
        "threshold_breached": breached,
        "new_pod": new_pod,
        "node": phase.node,
        "baseline_replica_count": len(baseline_names),
        "request_success": gen.success,
        "request_errors": gen.errors,
        "request_status_counts": dict(gen.status_counts),
        "phase_timestamps": {k: v for k, v in phase.timestamps.items()
                             if not k.startswith("_")},
        "phase_deltas_seconds": {
            label.strip().split()[0]: val for label, _, val in deltas
        },
    }
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        log(f"wrote report to {args.json_out}", level="ok")

    return 2 if breached else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted", level="warn")
        sys.exit(130)
