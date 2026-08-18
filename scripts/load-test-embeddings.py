#!/usr/bin/env python3
"""
Embedding-endpoint load test — find the throughput ceiling and identify
the bottleneck stage (client / auth-proxy / OpenAiIngress / LLMServer).

Drives concurrent /v1/embeddings POSTs at a controlled concurrency, with
a configurable batch size. Reports observed throughput in
chunks-per-second, p50/p95/p99 latency, and (optionally) per-stage
in-flight counts polled from the Ray Serve dashboard mid-test.

Usage:

  # baseline: 10 concurrent threads, 32 inputs/batch, 60s
  ./scripts/load-test-embeddings.py

  # scan concurrency to find the saturation point:
  ./scripts/load-test-embeddings.py --sweep 1,4,16,64,256

  # large batch, push the server to its limit:
  ./scripts/load-test-embeddings.py --concurrency 128 --batch-size 128 --duration 120

  # measure baseline at the internal LB vs the external gateway:
  ./scripts/load-test-embeddings.py --api-url https://models.example.com  # external
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Filler phrases — concatenated at startup to build chunks of the
# target token size. We don't tokenize client-side; tokens-to-chars is
# approximated at ~4 chars/token (typical for English with BERT-style
# tokenizers, including mxbai's). Real chunks from ingestion pipelines
# are typically 200-500 tokens, so default chunk_tokens=250 mirrors
# typical production traffic.
FILLER_PHRASES = [
    "Distributed systems require careful design to handle partial failures gracefully and maintain consistency under network partitions. ",
    "GPU autoscaling balances inference latency, throughput, and cloud cost across heterogeneous workloads with varying resource needs. ",
    "Embedding models map natural language text into dense vector spaces where semantic similarity becomes geometric proximity. ",
    "Vector similarity search using cosine distance or dot product enables scalable retrieval over billions of embedded documents. ",
    "Transformer attention runs in time and memory quadratic to the input sequence length without specialized optimizations. ",
    "Quantization reduces model memory footprint and inference latency at modest quality cost when implemented with calibration. ",
    "Tokenization splits raw text into discrete units recognized by the model vocabulary, balancing coverage and granularity. ",
    "Batch inference amortizes GPU compute setup costs across many simultaneous requests when workload arrival rate permits. ",
    "Kubernetes orchestrates containerized workloads with declarative configuration, automated scheduling, and self-healing behaviors. ",
    "Ray Serve provides a flexible Python-native framework for deploying machine learning models as scalable HTTP services. ",
]


def build_chunks(chunk_tokens: int, n_variants: int = 16) -> list[str]:
    """Build n_variants distinct strings each ~chunk_tokens long.
    Approximates 1 token ≈ 4 chars (English BERT-style tokenizers).
    Variants are made distinct by prepending a unique sentinel so the
    server's tokenizer can't collide them — useful when batch_size
    exceeds the variant pool."""
    target_chars = chunk_tokens * 4
    chunks = []
    for v in range(n_variants):
        # Per-variant sentinel + rotated phrase order so each chunk is
        # syntactically distinct and traversed by different attention
        # patterns inside the model.
        sentinel = f"Variant {v:03d}. "
        ordered = (FILLER_PHRASES[v % len(FILLER_PHRASES):]
                   + FILLER_PHRASES[:v % len(FILLER_PHRASES)])
        text = sentinel
        while len(text) < target_chars:
            for phrase in ordered:
                text += phrase
                if len(text) >= target_chars:
                    break
        chunks.append(text[:target_chars].rstrip() + ".")
    return chunks


@dataclass
class Result:
    success: int = 0
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)  # seconds, end-to-end
    # Per-stage latency captured only in with_tokenize mode.
    embed_latencies: list[float] = field(default_factory=list)
    tokenize_latencies: list[float] = field(default_factory=list)
    detokenize_latencies: list[float] = field(default_factory=list)
    chunks: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _post_json(url: str, headers: dict, payload: dict, timeout: float = 30) -> tuple[int, dict]:
    """POST a JSON body and return (status_code, parsed_response). Raises on
    transport errors; caller is responsible for catching."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        status = r.status
    return status, json.loads(body)


def worker(stop_event: threading.Event, embed_url: str, tokenize_url: str,
           detokenize_url: str, headers: dict, model: str, batch_size: int,
           chunks: list[str], with_tokenize: bool, result: Result) -> None:
    while not stop_event.is_set():
        # Pick batch_size chunks per request; sample with replacement so the
        # request shape is consistent even when batch_size > len(chunks).
        inputs = [random.choice(chunks) for _ in range(batch_size)]
        t0 = time.monotonic()
        status = 0
        try:
            if with_tokenize:
                # Per-chunk tokenize → detokenize round-trip BEFORE embedding.
                # This simulates a hypothetical client that pre-truncates
                # against the tokenizer service. Sequential per chunk in the
                # batch, then a single batched /v1/embeddings call.
                final_inputs = []
                tok_total = 0.0
                detok_total = 0.0
                for chunk_text in inputs:
                    tk0 = time.monotonic()
                    _, tok_resp = _post_json(tokenize_url, headers,
                                             {"model": model, "prompt": chunk_text}, timeout=15)
                    tok_total += time.monotonic() - tk0
                    tokens = tok_resp.get("tokens", [])
                    # Cap to max_model_len if the server reported one.
                    mml = tok_resp.get("max_model_len")
                    if isinstance(mml, int) and len(tokens) > mml:
                        tokens = tokens[:mml]
                    dt0 = time.monotonic()
                    _, detok_resp = _post_json(detokenize_url, headers,
                                               {"model": model, "tokens": tokens}, timeout=15)
                    detok_total += time.monotonic() - dt0
                    final_inputs.append(detok_resp.get("prompt", chunk_text))
                em0 = time.monotonic()
                status, _ = _post_json(embed_url, headers,
                                       {"model": model, "input": final_inputs}, timeout=60)
                embed_elapsed = time.monotonic() - em0
            else:
                tok_total = 0.0
                detok_total = 0.0
                em0 = time.monotonic()
                status, _ = _post_json(embed_url, headers,
                                       {"model": model, "input": inputs}, timeout=30)
                embed_elapsed = time.monotonic() - em0
            elapsed = time.monotonic() - t0
            with result.lock:
                result.success += 1
                result.chunks += batch_size
                result.latencies.append(elapsed)
                result.embed_latencies.append(embed_elapsed)
                if with_tokenize:
                    result.tokenize_latencies.append(tok_total)
                    result.detokenize_latencies.append(detok_total)
                result.status_counts[status] = result.status_counts.get(status, 0) + 1
        except urllib.error.HTTPError as e:
            with result.lock:
                result.errors += 1
                result.status_counts[e.code] = result.status_counts.get(e.code, 0) + 1
        # ssl.SSLError is OSError, not URLError — keeping it under the generic
        # transport catch keeps the worker alive across transient TLS hiccups
        # (e.g. SSLV3_ALERT_BAD_RECORD_MAC under high concurrency against the
        # GKE Gateway). Without this the thread would die uncaught and the
        # phase would silently lose throughput.
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            with result.lock:
                result.errors += 1
                # categorize transport errors under status 0
                result.status_counts[0] = result.status_counts.get(0, 0) + 1
        except Exception:
            # final backstop — anything else still counts as an error rather
            # than killing the worker thread mid-test.
            with result.lock:
                result.errors += 1
                result.status_counts[-1] = result.status_counts.get(-1, 0) + 1


def run_one(api_url: str, api_key: str, model: str, concurrency: int,
            batch_size: int, duration: int, chunks: list[str],
            with_tokenize: bool = False) -> Result:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    base = api_url.rstrip("/")
    embed_url = base + "/v1/embeddings"
    tokenize_url = base + "/tokenize"
    detokenize_url = base + "/detokenize"
    result = Result()
    stop_event = threading.Event()
    threads = [
        threading.Thread(target=worker,
                         args=(stop_event, embed_url, tokenize_url, detokenize_url,
                               headers, model, batch_size, chunks, with_tokenize, result),
                         daemon=True)
        for _ in range(concurrency)
    ]
    for t in threads:
        t.start()
    time.sleep(duration)
    stop_event.set()
    for t in threads:
        t.join(timeout=5)
    return result


def summarize(result: Result, duration: int, concurrency: int, batch_size: int) -> dict:
    total_reqs = result.success + result.errors
    rps = result.success / duration
    chunks_per_sec = result.chunks / duration
    s = {
        "concurrency": concurrency,
        "batch_size": batch_size,
        "duration_s": duration,
        "total_requests": total_reqs,
        "success": result.success,
        "errors": result.errors,
        "status_counts": dict(result.status_counts),
        "rps_success": round(rps, 1),
        "chunks_per_sec": round(chunks_per_sec, 1),
    }
    if result.latencies:
        result.latencies.sort()
        s["latency_p50_ms"] = round(statistics.median(result.latencies) * 1000, 1)
        s["latency_p95_ms"] = round(
            result.latencies[int(len(result.latencies) * 0.95) - 1] * 1000, 1)
        s["latency_p99_ms"] = round(
            result.latencies[int(len(result.latencies) * 0.99) - 1] * 1000, 1)
        s["latency_mean_ms"] = round(statistics.mean(result.latencies) * 1000, 1)
    # Per-stage breakdown is populated only when --with-tokenize is on.
    # Each value is the per-request *total* time spent in that stage
    # (sum across all chunks in the batch for tokenize/detokenize, single
    # call for embed). Means / medians give a clear attribution of where
    # the wall time went.
    for stage, vals in (("embed", result.embed_latencies),
                        ("tokenize", result.tokenize_latencies),
                        ("detokenize", result.detokenize_latencies)):
        if vals:
            vals.sort()
            s[f"{stage}_mean_ms"] = round(statistics.mean(vals) * 1000, 1)
            s[f"{stage}_p50_ms"] = round(statistics.median(vals) * 1000, 1)
            s[f"{stage}_p95_ms"] = round(vals[int(len(vals) * 0.95) - 1] * 1000, 1)
    return s


def fetch_inflight(context: str | None) -> dict:
    """Poll Ray Serve dashboard for per-deployment in-flight counts.
    Returns {} if context is None or kubectl/port-forward fails."""
    if not context:
        return {}
    try:
        head_pod = subprocess.check_output(
            ["kubectl", "--context", context, "-n",
             os.environ.get("NAMESPACE", "open-serve"),
             "get", "pods", "-l", "ray.io/node-type=head", "-o", "name"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        head_pod = next((p.replace("pod/", "") for p in head_pod), None)
        if not head_pod:
            return {}
        out = subprocess.check_output(
            ["kubectl", "--context", context, "-n",
             os.environ.get("NAMESPACE", "open-serve"),
             "exec", head_pod, "-c", "ray-head", "--",
             "python3", "-c",
             "import urllib.request, json; "
             "d=json.loads(urllib.request.urlopen("
             "'http://localhost:8265/api/serve/applications/', timeout=5).read()); "
             "out={};\n"
             "[out.setdefault(dep, []).append(r.get('state')) "
             "for app in d['applications'].values() "
             "for dep, depinfo in app['deployments'].items() "
             "for r in depinfo.get('replicas', [])];\n"
             "print(json.dumps(out))"],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        )
        return json.loads(out)
    except Exception:
        return {}


def main() -> int:
    p = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=__doc__)
    p.add_argument("--api-url", default=os.environ.get("BASE_URL"),
                   help="gateway base URL (or set BASE_URL env var; required)")
    p.add_argument("--api-key", default=None,
                   help="bearer token (or set API_KEY env var)")
    p.add_argument("--api-key-file", default=None,
                   help="read API key from this file")
    p.add_argument("--model", default="mixedbread-ai/mxbai-embed-large-v1")
    p.add_argument("--concurrency", type=int, default=10,
                   help="concurrent worker threads")
    p.add_argument("--batch-size", type=int, default=32,
                   help="inputs per /v1/embeddings request")
    p.add_argument("--chunk-tokens", type=int, default=250,
                   help="approximate token count per chunk (default 250, "
                        "matches typical ingestion traffic). Capped at "
                        "the model's max_model_len server-side.")
    p.add_argument("--duration", type=int, default=60,
                   help="seconds per phase")
    p.add_argument("--sweep", default=None,
                   help="comma-separated list of concurrencies to run sequentially "
                        "(e.g. 1,4,16,64,256). Overrides --concurrency.")
    p.add_argument("--warmup", type=int, default=10,
                   help="seconds of warmup per phase (not counted)")
    p.add_argument("--context", default=None,
                   help="kubectl context for in-cluster diagnostics "
                        "(if set, polls Ray Serve dashboard for replica states)")
    p.add_argument("--json-out", default=None,
                   help="write structured report to this path")
    p.add_argument("--with-tokenize", action="store_true",
                   help="for each chunk in the batch, call /tokenize then "
                        "/detokenize BEFORE the batched /v1/embeddings call. "
                        "Simulates a hypothetical client that pre-truncates "
                        "against the tokenizer service. Sequential per chunk; "
                        "captures per-stage latency breakdown.")
    args = p.parse_args()

    if not args.api_url:
        print("error: --api-url (or BASE_URL env var) required", file=sys.stderr)
        return 2

    api_key = args.api_key or os.environ.get("API_KEY")
    if args.api_key_file:
        api_key = open(args.api_key_file).read().strip()
    if not api_key:
        print("error: --api-key, --api-key-file, or API_KEY required",
              file=sys.stderr)
        return 2

    concurrencies = (
        [int(x) for x in args.sweep.split(",")] if args.sweep else [args.concurrency]
    )

    # Build chunks once — every phase uses the same set so results compare.
    chunks = build_chunks(args.chunk_tokens)
    actual_chars = len(chunks[0])
    approx_tokens = actual_chars // 4
    print(f"chunk_tokens={args.chunk_tokens} (target) → {actual_chars} chars "
          f"≈ {approx_tokens} tokens (1 token ≈ 4 chars approximation)")

    results = []
    mode = "embed+tokenize+detokenize" if args.with_tokenize else "embed-only"
    for c in concurrencies:
        print(f"\n=== concurrency={c} batch_size={args.batch_size} "
              f"chunk_tokens≈{approx_tokens} duration={args.duration}s "
              f"mode={mode} url={args.api_url} ===")
        # Warmup
        if args.warmup > 0:
            print(f"  warmup {args.warmup}s ...")
            run_one(args.api_url, api_key, args.model, c, args.batch_size,
                    args.warmup, chunks, with_tokenize=args.with_tokenize)
        # Measured run
        t_start = time.monotonic()
        result = run_one(args.api_url, api_key, args.model, c, args.batch_size,
                         args.duration, chunks, with_tokenize=args.with_tokenize)
        elapsed = time.monotonic() - t_start
        s = summarize(result, args.duration, c, args.batch_size)
        s["chunk_tokens_approx"] = approx_tokens
        s["wall_seconds"] = round(elapsed, 1)
        s["mode"] = mode
        tokens_per_sec = s["chunks_per_sec"] * approx_tokens
        s["tokens_per_sec"] = round(tokens_per_sec, 1)
        print(f"  reqs={s['total_requests']} ok={s['success']} err={s['errors']}")
        print(f"  rps={s['rps_success']} chunks/s={s['chunks_per_sec']} "
              f"tokens/s={s['tokens_per_sec']:.0f}")
        if "latency_p50_ms" in s:
            print(f"  latency p50/p95/p99 = "
                  f"{s['latency_p50_ms']:.0f}/{s['latency_p95_ms']:.0f}/"
                  f"{s['latency_p99_ms']:.0f}ms")
        if args.with_tokenize and "embed_p50_ms" in s:
            print(f"  per-stage p50  tok/embed/detok = "
                  f"{s.get('tokenize_p50_ms', 0):.0f}/"
                  f"{s.get('embed_p50_ms', 0):.0f}/"
                  f"{s.get('detokenize_p50_ms', 0):.0f}ms")
        print(f"  status: {s['status_counts']}")
        # Snapshot replica state (best-effort)
        states = fetch_inflight(args.context)
        if states:
            for dep, replica_states in states.items():
                running = sum(1 for x in replica_states if x == "RUNNING")
                print(f"  ray-serve: {dep}: {running}/{len(replica_states)} RUNNING")
        results.append(s)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"runs": results, "model": args.model,
                       "api_url": args.api_url, "mode": mode}, f, indent=2)
        print(f"\nwrote {args.json_out}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
