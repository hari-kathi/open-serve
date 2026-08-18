"""Resolve a `model_source` URI to a local directory of model weights.

Import-safe by construction: no ray/vllm/GPU dependencies and no
top-level heavy imports. `fsspec` and `huggingface_hub` are imported
lazily inside the functions that need them, so `import model_source`
(and the Dockerfile's build-time import smoke check) works in a bare
Python environment.

Supported source forms:

- local path (no ``scheme://``) — returned as-is; vLLM opens it directly.
- ``hf://org/name`` — downloaded via ``huggingface_hub.snapshot_download``
  into ``<cache_root>/hf/org/name``. ``HF_TOKEN`` in the environment is
  picked up implicitly by huggingface_hub for gated/private repos.
- any other ``scheme://`` (``gs://``, ``s3://``, ``az://``, ``abfs://``,
  ``file://``, ``memory://``, …) — mirrored recursively via the matching
  fsspec filesystem into ``<cache_root>/<scheme>/<path>``, preserving the
  remote relative layout. Files already present locally at the same size
  are skipped, so re-resolving after a replica restart in the same pod
  reuses the cache instead of re-downloading.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

# Default local cache root for mirrored weights. Persists for the worker
# pod's lifetime so a Ray Serve replica restart inside the same pod
# reuses the download instead of re-fetching.
DEFAULT_CACHE_ROOT = "/tmp/models"

# Maps fsspec protocol → pip package (the "extra") that provides the
# driver, used to produce an actionable error when it's not installed.
_DRIVER_PACKAGES = {
    "gs": "gcsfs",
    "gcs": "gcsfs",
    "s3": "s3fs",
    "s3a": "s3fs",
    "az": "adlfs",
    "abfs": "adlfs",
    "abfss": "adlfs",
    "adl": "adlfs",
}


def resolve_model_source(source: str, cache_root: str = DEFAULT_CACHE_ROOT) -> str:
    """Resolve *source* to a local directory path, mirroring if remote.

    Returns the local path vLLM should use as ``--model``. See module
    docstring for the supported source forms and caching semantics.
    """
    if not isinstance(source, str):
        raise ValueError(f"model_source must be a string URI, got {type(source).__name__}")
    if "://" not in source:
        # Local path — assume it's already accessible and let vLLM open it.
        return source
    scheme, _, path = source.partition("://")
    if scheme == "hf":
        return _resolve_hf(path, cache_root)
    return _mirror_with_fsspec(scheme, path, source, cache_root)


def _resolve_hf(repo_id: str, cache_root: str) -> str:
    """Download an ``hf://org/name`` snapshot into <cache_root>/hf/<repo_id>."""
    repo_id = repo_id.strip("/")
    if not repo_id:
        raise ValueError("hf:// model_source must name a repo, e.g. hf://org/name")
    local_dir = os.path.join(cache_root, "hf", repo_id)
    # Lazy import: huggingface_hub is only needed for hf:// sources.
    from huggingface_hub import snapshot_download

    # snapshot_download is idempotent (resumes/reuses existing files) and
    # reads HF_TOKEN from the environment implicitly for gated repos.
    snapshot_download(repo_id=repo_id, local_dir=local_dir)
    _log.info("model_source hf://%s downloaded to %s", repo_id, local_dir)
    return local_dir


def _mirror_with_fsspec(scheme: str, path: str, source: str, cache_root: str) -> str:
    """Recursively mirror ``scheme://path`` into <cache_root>/<scheme>/<path>."""
    # Lazy import: keeps `import model_source` dependency-free.
    import fsspec

    try:
        fs = fsspec.filesystem(scheme)
    except ImportError as exc:
        package = _DRIVER_PACKAGES.get(scheme)
        hint = (
            f"install it with `pip install {package}`"
            if package
            else f"install the fsspec driver package that provides '{scheme}'"
        )
        raise RuntimeError(
            f"model_source {source!r} needs the fsspec '{scheme}' filesystem "
            f"driver, which is not installed — {hint}"
        ) from exc

    remote_root = fs._strip_protocol(source)
    dest_dir = os.path.join(cache_root, scheme, path.strip("/"))
    os.makedirs(dest_dir, exist_ok=True)

    remote_files = [f for f in fs.find(remote_root) if not f.endswith("/")]
    if not remote_files:
        raise FileNotFoundError(
            f"no files at {source}; check the bucket/prefix and the pod identity's read permissions"
        )

    total_bytes = 0
    skipped = 0
    for rpath in remote_files:
        rel = rpath[len(remote_root):].lstrip("/") if rpath.startswith(remote_root) else rpath.lstrip("/")
        if not rel:
            # source pointed at a single file rather than a prefix.
            rel = os.path.basename(rpath)
        dst = os.path.join(dest_dir, rel)
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        size = fs.info(rpath).get("size") or 0
        # Idempotency: skip files already mirrored at the expected size,
        # so a replica restart in the same pod reuses the cache.
        if os.path.exists(dst) and os.path.getsize(dst) == size:
            skipped += 1
            continue
        fs.get_file(rpath, dst)
        total_bytes += size

    _log.info(
        "model_source %s mirrored to %s: downloaded=%d bytes, skipped=%d files",
        source, dest_dir, total_bytes, skipped,
    )
    return dest_dir
