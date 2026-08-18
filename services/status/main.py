"""open-serve status page — public health view modeled on status.huggingface.co
and status.openai.com.

v2 design:
  - One row per model, expandable into internal + external sub-rows
  - Hourly uptime strip (one bar per UTC hour) over a STRIP_DAYS window;
    horizontally scrollable since the full window won't fit on screen
  - "Recent History" section: daily cards with any incidents that occurred
  - 3-tier color: green (healthy) / orange (degraded) / red (down)
  - >=2 consecutive failed probe samples required to count as an incident
    (filters out single-probe blips from rolling KubeRay swaps)
  - Endpoint state inherits "internal" failures as user-facing — most
    consumers are internal, so we surface them
  - Serves stale snapshot when Prometheus is briefly unreachable; falls
    back to "unknown" only on cold-cache failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from prometheus_client import Counter, Histogram, generate_latest

logger = logging.getLogger("openserve-status")

# --- Configuration ---

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.open-serve.svc:9090",
)
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "30"))
# Strip window length in days. Each bar is one UTC hour, so the strip renders
# STRIP_DAYS * 24 bars and scrolls horizontally (latest on the right). Kept
# in days because that's the natural retention/history unit; default 14 keeps
# the bar count (~336) and DOM weight reasonable at hourly granularity.
STRIP_DAYS = int(os.environ.get("STRIP_DAYS", "14"))
RECENT_HISTORY_DAYS = int(os.environ.get("RECENT_HISTORY_DAYS", "30"))
INCIDENT_MIN_CONSECUTIVE_SAMPLES = int(os.environ.get("INCIDENT_MIN_CONSECUTIVE_SAMPLES", "2"))
PROMETHEUS_TIMEOUT_S = float(os.environ.get("PROMETHEUS_TIMEOUT_S", "3.0"))
INCIDENT_QUERY_TIMEOUT_S = float(os.environ.get("INCIDENT_QUERY_TIMEOUT_S", "10.0"))
DEFAULT_LATENCY_SLO_S = float(os.environ.get("DEFAULT_LATENCY_SLO_S", "10.0"))

# Probe cadence — drives the granularity at which we sample probe success
# in Prometheus query_range calls. Should match probe.intervalSeconds in
# the probe deployment's config (default 900 = 15 min).
PROBE_INTERVAL_S = int(os.environ.get("PROBE_INTERVAL_S", "900"))

BRANDING_TITLE = os.environ.get("BRANDING_TITLE", "open-serve Status")
BRANDING_LOGO_URL = os.environ.get("BRANDING_LOGO_URL", "")
BRANDING_FOOTER_LINK_TEXT = os.environ.get("BRANDING_FOOTER_LINK_TEXT", "")
BRANDING_FOOTER_LINK_URL = os.environ.get("BRANDING_FOOTER_LINK_URL", "")

MODEL_METADATA_FILE = os.environ.get("MODEL_METADATA_FILE", "/config/model-metadata.json")
SLO_CONFIG_FILE = os.environ.get("SLO_CONFIG_FILE", "/config/slo-config.json")
CATEGORY_ORDER_FILE = os.environ.get("CATEGORY_ORDER_FILE", "/config/category-order.json")

# Default category-section ordering. Operator-overridable via the
# category-order.json config file. Categories not in the list render
# alphabetically after the listed ones.
DEFAULT_CATEGORY_ORDER = [
    "LLM",
    "Vision-Language",
    "Embedding",
    "Reranker",
    "OCR",
    "Audio",
    "Model",
    "Other",
]

TIER_FILTER = 'tier="production"'


# --- Three-tier health state ---

class State(IntEnum):
    HEALTHY = 0
    DEGRADED = 1
    DOWN = 2
    UNKNOWN = 3

    @property
    def label(self) -> str:
        return {0: "healthy", 1: "degraded", 2: "down", 3: "unknown"}[self.value]

    @property
    def color(self) -> str:
        # DEGRADED uses a darker yellow (yellow-600) rather than an orange
        # so it's clearly distinct from DOWN red — orange and red read as
        # similar at small bar sizes.
        return {0: "#22c55e", 1: "#ca8a04", 2: "#ef4444", 3: "#94a3b8"}[self.value]


# --- Domain types ---

@dataclass
class EndpointHealth:
    """One probe target (model × endpoint pair)."""
    endpoint: str  # "internal" | "external"
    state: State
    success_rate_5m: float | None
    p95_latency_s: float | None
    slo_latency_s: float
    hourly_strip: list[HourCell] = field(default_factory=list)


@dataclass
class HourCell:
    """One bar in the hourly uptime strip (a single UTC hour)."""
    iso_hour: str  # YYYY-MM-DDTHH:00Z (UTC hour start)
    state: State
    uptime_fraction: float | None  # 0.0-1.0 (or None for no-data hours)
    detail: str | None = None  # why a non-healthy hour is non-healthy (tooltip)

    @property
    def iso_date(self) -> str:
        """YYYY-MM-DD — used to group bars into per-day segments on the strip."""
        return self.iso_hour[:10]

    @property
    def hour(self) -> int:
        return int(self.iso_hour[11:13])

    @property
    def is_day_start(self) -> bool:
        """True for the 00:00 bar — the template draws a day divider here so a
        scrolled strip stays oriented across midnight boundaries."""
        return self.hour == 0


@dataclass
class Model:
    """A model row with one or more probe endpoints."""
    model_id: str
    category: str  # "Model", "Embedding", "Vision", etc.
    description: str
    endpoints: list[EndpointHealth] = field(default_factory=list)

    @property
    def state(self) -> State:
        """Top-level state = worst across endpoints (internal AND external)."""
        if not self.endpoints:
            return State.UNKNOWN
        return State(max(e.state.value for e in self.endpoints))

    @property
    def uptime_fraction(self) -> float | None:
        """Mean uptime across the strip window, averaged across endpoints."""
        fractions: list[float] = []
        for ep in self.endpoints:
            present = [c.uptime_fraction for c in ep.hourly_strip if c.uptime_fraction is not None]
            if present:
                fractions.append(sum(present) / len(present))
        if not fractions:
            return None
        return sum(fractions) / len(fractions)

    @property
    def hourly_strip(self) -> list[HourCell]:
        """Top-level strip = per-hour worst state across endpoints.

        Each cell takes the worst state observed across internal+external in
        that hour, and uptime as the min uptime across endpoints.
        """
        if not self.endpoints:
            return []
        by_hour: dict[str, HourCell] = {}
        for ep in self.endpoints:
            for cell in ep.hourly_strip:
                cur = by_hour.get(cell.iso_hour)
                if cur is None:
                    by_hour[cell.iso_hour] = HourCell(
                        cell.iso_hour, cell.state, cell.uptime_fraction, cell.detail
                    )
                    continue
                # Worst state wins (and carries its reason); lower uptime wins.
                if cell.state.value > cur.state.value:
                    cur.state = cell.state
                    cur.detail = cell.detail
                if cell.uptime_fraction is not None:
                    if cur.uptime_fraction is None or cell.uptime_fraction < cur.uptime_fraction:
                        cur.uptime_fraction = cell.uptime_fraction
        return sorted(by_hour.values(), key=lambda c: c.iso_hour)


@dataclass
class Incident:
    model_id: str
    endpoint: str
    start_ts: float
    end_ts: float | None
    peak_state: State
    error_type: str | None = None  # most-frequent error_type during the incident

    @property
    def duration_s(self) -> float:
        return (self.end_ts or time.time()) - self.start_ts

    @property
    def is_active(self) -> bool:
        return self.end_ts is None

    @property
    def start_date(self) -> str:
        return datetime.fromtimestamp(self.start_ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class DailyHistoryEntry:
    iso_date: str
    incidents: list[Incident]


@dataclass
class PageSnapshot:
    rendered_at: float
    banner_state: State
    fallback_mode: bool
    data_stale_s: float  # 0 on fresh fetch; >0 if served from prior cache
    models_by_category: dict[str, list[Model]]
    active_incidents: list[Incident]
    recent_history: list[DailyHistoryEntry]
    branding: dict[str, str]


# --- State classifier ---

def classify(
    success_rate_5m: float | None,
    p95_latency_s: float | None,
    slo_latency_s: float,
    *,
    success_threshold_down: float = 0.5,
    success_threshold_degraded: float = 0.99,
) -> State:
    """Map raw probe signals to a three-tier state.

    Stale-last-success used to be part of the rule; we dropped it in v2
    because Prometheus already ages out the metric when the probe stops
    publishing — UNKNOWN is the right state in that case.
    """
    if success_rate_5m is None:
        return State.UNKNOWN
    if success_rate_5m < success_threshold_down:
        return State.DOWN
    if success_rate_5m < success_threshold_degraded:
        return State.DEGRADED
    if p95_latency_s is not None and p95_latency_s > slo_latency_s:
        return State.DEGRADED
    return State.HEALTHY


def banner_from(states: list[State]) -> State:
    if not states:
        return State.HEALTHY
    return State(max(s.value for s in states))


# --- Prometheus client ---

class PrometheusClient:
    def __init__(self, base_url: str, *, timeout_s: float = PROMETHEUS_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def _get(
        self,
        path: str,
        params: dict[str, Any],
        timeout_s: float | None = None,
    ) -> dict[str, Any] | None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout_s or self._timeout)
        try:
            resp = await self._client.get(
                f"{self.base_url}{path}",
                params=params,
                timeout=timeout_s or self._timeout,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            logger.warning("prometheus %s failed: %s", path, exc)
            return None
        if resp.status_code != 200:
            logger.warning("prometheus %s returned %d", path, resp.status_code)
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        if data.get("status") != "success":
            return None
        return data.get("data") or {}

    async def instant(self, query: str) -> list[dict[str, Any]] | None:
        data = await self._get("/api/v1/query", {"query": query})
        if data is None:
            return None
        result = data.get("result")
        return result if isinstance(result, list) else []

    async def range(
        self,
        query: str,
        start_ts: float,
        end_ts: float,
        step_s: int,
        *,
        timeout_s: float | None = None,
    ) -> list[dict[str, Any]] | None:
        data = await self._get(
            "/api/v1/query_range",
            {"query": query, "start": str(start_ts), "end": str(end_ts), "step": str(step_s)},
            timeout_s=timeout_s,
        )
        if data is None:
            return None
        result = data.get("result")
        return result if isinstance(result, list) else []

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


# --- Snapshotter ---

class Snapshotter:
    def __init__(
        self,
        prom: PrometheusClient,
        model_metadata: dict[str, dict[str, str]],
        slo_per_model: dict[str, float],
        *,
        cache_ttl_s: int = CACHE_TTL_SECONDS,
        strip_days: int = STRIP_DAYS,
        recent_history_days: int = RECENT_HISTORY_DAYS,
        incident_min_consecutive_samples: int = INCIDENT_MIN_CONSECUTIVE_SAMPLES,
        probe_interval_s: int = PROBE_INTERVAL_S,
        category_order: list[str] | None = None,
    ) -> None:
        self.prom = prom
        self.model_metadata = model_metadata
        self.slo_per_model = slo_per_model
        self.cache_ttl_s = cache_ttl_s
        self.strip_days = strip_days
        self.recent_history_days = recent_history_days
        self.incident_min_consecutive_samples = incident_min_consecutive_samples
        self.probe_interval_s = probe_interval_s
        self.category_order = category_order if category_order is not None else list(DEFAULT_CATEGORY_ORDER)
        self._cache: PageSnapshot | None = None
        self._build_lock = asyncio.Lock()

    async def get(self) -> PageSnapshot:
        async with self._build_lock:
            if self._cache is not None and (time.time() - self._cache.rendered_at) < self.cache_ttl_s:
                return self._cache
            fresh = await self._build()
            if fresh is not None:
                self._cache = fresh
                return fresh
            # Build failed — serve stale cache if we have one, otherwise fallback.
            if self._cache is not None:
                stale_age = time.time() - self._cache.rendered_at
                logger.warning(
                    "serving stale snapshot (age=%.0fs) — prometheus unreachable",
                    stale_age,
                )
                return PageSnapshot(
                    rendered_at=self._cache.rendered_at,
                    banner_state=self._cache.banner_state,
                    fallback_mode=False,
                    data_stale_s=stale_age,
                    models_by_category=self._cache.models_by_category,
                    active_incidents=self._cache.active_incidents,
                    recent_history=self._cache.recent_history,
                    branding=self._cache.branding,
                )
            self._cache = self._cold_fallback()
            return self._cache

    def _slo_for(self, model_id: str) -> float:
        return self.slo_per_model.get(model_id, DEFAULT_LATENCY_SLO_S)

    def _order_categories(self, unsorted: dict[str, list[Model]]) -> dict[str, list[Model]]:
        """Sort categories per self.category_order; alphabetical fallback for the rest.

        Stable ordering matters so when operators add new model types (rerankers,
        OCR, audio) the page layout stays predictable across reloads.
        """
        ordered: dict[str, list[Model]] = {}
        seen: set[str] = set()
        for cat in self.category_order:
            if cat in unsorted:
                ordered[cat] = unsorted[cat]
                seen.add(cat)
        for cat in sorted(unsorted.keys()):
            if cat not in seen:
                ordered[cat] = unsorted[cat]
        return ordered

    def _meta_for(self, model_id: str) -> dict[str, str]:
        return self.model_metadata.get(model_id, {})

    async def _build(self) -> PageSnapshot | None:
        """Build a fresh snapshot. Returns None if Prometheus is unreachable."""
        now = time.time()

        # Wrap success in min by (model, endpoint) so probe-pod restarts
        # don't return multiple series per (model, endpoint) — see
        # _build_hourly_strip for the full explanation. Latency's
        # histogram_quantile already aggregates with `by` so no change needed.
        success_q = (
            'min by (model, endpoint) ('
            f'avg_over_time(openserve_probe_success{{{TIER_FILTER}}}[5m])'
            ')'
        )
        latency_q = (
            'histogram_quantile(0.95, '
            f'sum(rate(openserve_probe_latency_seconds_bucket{{{TIER_FILTER}}}[10m])) '
            'by (le, model, endpoint))'
        )
        success, latency = await asyncio.gather(
            self.prom.instant(success_q),
            self.prom.instant(latency_q),
        )
        if success is None:
            return None

        signals = self._merge_signals(success, latency)

        # Hourly uptime strip — aggregate probe samples to per-hour cells.
        strip_data = await self._build_hourly_strip(now)

        # Build models grouped by category, each with endpoints.
        models_by_id: dict[str, Model] = {}
        for (model_id, endpoint), values in sorted(signals.items()):
            slo = self._slo_for(model_id)
            state = classify(values.get("success_rate_5m"), values.get("p95_latency_s"), slo)
            ep = EndpointHealth(
                endpoint=endpoint,
                state=state,
                success_rate_5m=values.get("success_rate_5m"),
                p95_latency_s=values.get("p95_latency_s"),
                slo_latency_s=slo,
                hourly_strip=strip_data.get((model_id, endpoint), []),
            )
            if model_id not in models_by_id:
                meta = self._meta_for(model_id)
                models_by_id[model_id] = Model(
                    model_id=model_id,
                    category=meta.get("category", "Model"),
                    description=meta.get("description", ""),
                    endpoints=[],
                )
            models_by_id[model_id].endpoints.append(ep)

        # Bucket models by category; sort categories per the configured order.
        unsorted: dict[str, list[Model]] = {}
        for m in sorted(models_by_id.values(), key=lambda m: m.model_id):
            # Endpoints sorted so "external" comes before "internal" in the UI.
            m.endpoints.sort(key=lambda e: (e.endpoint != "external", e.endpoint))
            unsorted.setdefault(m.category, []).append(m)
        models_by_category = self._order_categories(unsorted)

        incidents = await self._detect_incidents(now)
        active = [i for i in incidents if i.is_active]
        history = self._group_history_by_day([i for i in incidents if not i.is_active], now)

        banner = banner_from([m.state for ms in models_by_category.values() for m in ms])

        return PageSnapshot(
            rendered_at=now,
            banner_state=banner,
            fallback_mode=False,
            data_stale_s=0.0,
            models_by_category=models_by_category,
            active_incidents=active,
            recent_history=history,
            branding=self._branding(),
        )

    def _cold_fallback(self) -> PageSnapshot:
        logger.warning("cold-cache fallback: prometheus unreachable, no prior snapshot to serve")
        return PageSnapshot(
            rendered_at=time.time(),
            banner_state=State.UNKNOWN,
            fallback_mode=True,
            data_stale_s=0.0,
            models_by_category={},
            active_incidents=[],
            recent_history=[],
            branding=self._branding(),
        )

    def _branding(self) -> dict[str, str]:
        return {
            "title": BRANDING_TITLE,
            "logo_url": BRANDING_LOGO_URL,
            "footer_link_text": BRANDING_FOOTER_LINK_TEXT,
            "footer_link_url": BRANDING_FOOTER_LINK_URL,
        }

    @staticmethod
    def _merge_signals(
        success: list[dict[str, Any]],
        latency: list[dict[str, Any]] | None,
    ) -> dict[tuple[str, str], dict[str, float]]:
        merged: dict[tuple[str, str], dict[str, float]] = {}

        def key_of(entry: dict[str, Any]) -> tuple[str, str] | None:
            metric = entry.get("metric") or {}
            model = metric.get("model")
            endpoint = metric.get("endpoint")
            if not model or not endpoint:
                return None
            return (model, endpoint)

        def value_of(entry: dict[str, Any]) -> float | None:
            v = entry.get("value")
            if not v or len(v) < 2:
                return None
            try:
                return float(v[1])
            except (TypeError, ValueError):
                return None

        for entry in success:
            k = key_of(entry)
            if k is None:
                continue
            v = value_of(entry)
            if v is not None:
                merged.setdefault(k, {})["success_rate_5m"] = v

        for entry in (latency or []):
            k = key_of(entry)
            if k is None:
                continue
            v = value_of(entry)
            if v is not None and v == v:  # NaN != NaN
                merged.setdefault(k, {})["p95_latency_s"] = v

        return merged

    async def _build_hourly_strip(self, now: float) -> dict[tuple[str, str], list[HourCell]]:
        """Walk probe samples at the probe interval and bucket into hourly cells.

        One bar per UTC hour. We sample at the probe interval (the same
        resolution and raw query as `_detect_incidents`) rather than a coarser
        avg_over_time, so each hour bucket holds the individual probe outcomes
        (~4 at 15-min cadence). That lets `_classify_bucket` apply the same
        `incident_min_consecutive_samples` blip filter the incident detector
        uses, keeping the strip and the recent-history list in agreement: a
        lone failed probe in an hour stays HEALTHY (green) and produces no
        incident; >=2 consecutive failures colour the hour AND log an incident.

        Edge case: a 2-sample outage straddling an hour boundary lands one
        failure in each adjacent hour, so neither hour crosses the consecutive
        threshold on its own — the strip can read green while `_detect_incidents`
        (which walks the unbucketed series) still flags it. This was already
        true at the day boundary in the old daily-bar design; hourly bars just
        make it more frequent. It's a cosmetic boundary artifact, not a
        correctness issue — real outages span many consecutive samples.

        IMPORTANT — uses `min by (model, endpoint)` to collapse the implicit
        pod-identity label that Prometheus retains. The probe is a single-
        replica Deployment; restarts publish under a new pod identity, which
        Prometheus treats as a distinct time series. Without this aggregation
        the loop body `out[(model, endpoint)] = cells` overwrote the bucket
        on each entry and only the last series' samples survived — older
        hours rendered as "no data" grey, and the SAME hour could disagree
        with `_detect_incidents` (which independently walked every fragment).
        min is conservative: at any timestamp only one probe pod is alive,
        so it returns that pod's value; if two pods hypothetically publish
        the same metric simultaneously, the worst (most pessimistic) wins.

        Each hour's state = `_classify_bucket` over that hour's samples. Uptime
        = healthy samples / total samples in that hour.

        Latency-aware DEGRADED: an hour where the probes succeeded (so the
        success rule says HEALTHY) but p95 probe latency exceeded the model's
        SLO is upgraded to DEGRADED (yellow) — "up but slow". We fetch per-hour
        p95 latency alongside the success series and only ever promote HEALTHY →
        DEGRADED here; a real outage (DOWN) is never masked. This is the
        dominant source of yellow on the strip, since binary probe success
        rarely yields a degraded success bucket on its own. Note it's a
        bar/live-state signal only — slow-but-up hours are intentionally NOT
        logged as discrete incidents (those track outages, not slowness).
        """
        step_s = self.probe_interval_s
        start_ts = now - (self.strip_days * 86400) - step_s
        # p95 latency window: a couple of probe cadences so each sample sees a
        # few observations; bucketed to the same hours as the success series.
        lat_window_s = max(2 * step_s, 1800)
        success_data, latency_data = await asyncio.gather(
            self.prom.range(
                f'min by (model, endpoint) (openserve_probe_success{{{TIER_FILTER}}})',
                start_ts,
                now,
                step_s,
                timeout_s=INCIDENT_QUERY_TIMEOUT_S,
            ),
            self.prom.range(
                'histogram_quantile(0.95, '
                f'sum(rate(openserve_probe_latency_seconds_bucket{{{TIER_FILTER}}}[{lat_window_s}s])) '
                'by (le, model, endpoint))',
                start_ts,
                now,
                step_s,
                timeout_s=INCIDENT_QUERY_TIMEOUT_S,
            ),
        )
        if success_data is None:
            return {}
        # Latency is best-effort — if its query fails the bars fall back to
        # pure up/down (success-only) classification.
        latency_by_target = self._index_hourly_latency(latency_data or [])

        out: dict[tuple[str, str], list[HourCell]] = {}
        for entry in success_data:
            metric = entry.get("metric") or {}
            model = metric.get("model")
            endpoint = metric.get("endpoint")
            if not model or not endpoint:
                continue
            per_hour: dict[str, list[float]] = {}
            for sample in (entry.get("values") or []):
                if len(sample) < 2:
                    continue
                try:
                    ts = float(sample[0])
                    success = float(sample[1])
                except (TypeError, ValueError):
                    continue
                iso_hour = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z")
                per_hour.setdefault(iso_hour, []).append(success)

            slo = self._slo_for(model)
            lat_hours = latency_by_target.get((model, endpoint), {})
            cells: list[HourCell] = []
            for iso_hour in sorted(per_hour.keys()):
                samples = per_hour[iso_hour]
                state = self._classify_bucket(samples)
                healthy = sum(1 for s in samples if s >= 0.99)
                uptime = healthy / len(samples)
                p95 = lat_hours.get(iso_hour)
                detail: str | None = None
                # Up-but-slow → DEGRADED (only promotes HEALTHY; never masks DOWN).
                if state == State.HEALTHY and p95 is not None and p95 > slo:
                    state = State.DEGRADED
                    detail = (
                        f"Elevated latency · p95 {_format_latency(p95)} "
                        f"(SLO {_format_latency(slo)})"
                    )
                elif state == State.DEGRADED:
                    detail = "Intermittent probe failures"
                elif state == State.DOWN:
                    detail = "Probe failures (outage)"
                cells.append(
                    HourCell(iso_hour=iso_hour, state=state, uptime_fraction=uptime, detail=detail)
                )

            # Pad missing hours as no-data so the strip always has strip_days*24 cells.
            cells = self._pad_strip(cells, now)
            out[(model, endpoint)] = cells
        return out

    @staticmethod
    def _index_hourly_latency(
        data: list[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, float]]:
        """Index per-hour p95 latency as {(model, endpoint): {iso_hour: max_p95}}.

        Bucketed by the sample's UTC hour (same as the success series) so the
        latency and success views of an hour line up. Within an hour we keep the
        max p95 — if any window in the hour breached the SLO, the hour is slow.
        NaN samples (no histogram observations in the window) are skipped.
        """
        out: dict[tuple[str, str], dict[str, float]] = {}
        for entry in data:
            metric = entry.get("metric") or {}
            model = metric.get("model")
            endpoint = metric.get("endpoint")
            if not model or not endpoint:
                continue
            per_hour = out.setdefault((model, endpoint), {})
            for sample in (entry.get("values") or []):
                if len(sample) < 2:
                    continue
                try:
                    ts = float(sample[0])
                    v = float(sample[1])
                except (TypeError, ValueError):
                    continue
                if v != v:  # NaN — no observations in this window
                    continue
                iso_hour = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z")
                cur = per_hour.get(iso_hour)
                if cur is None or v > cur:
                    per_hour[iso_hour] = v
        return out

    def _classify_bucket(self, samples: list[float]) -> State:
        """Classify a bucket's probe-success samples into HEALTHY / DEGRADED / DOWN.

        A "bucket" is one hourly strip cell. Mirrors the incident detector's
        rule so the strip and the recent-history incident list never disagree
        on what happened. Specifically:

          - Find the longest run of *consecutive* non-healthy samples in
            this bucket. If that run meets the configured
            `incident_min_consecutive_samples` threshold (default 2), the
            bucket inherits that run's peak state (DEGRADED or DOWN).
          - Otherwise, if the bucket has <=1 isolated unhealthy sample, treat
            it as a transient blip (rolling KubeRay swap, brief network
            hiccup) and render HEALTHY.
          - Otherwise (multiple scattered failures with no qualifying run),
            render DEGRADED.

        Granularity-agnostic: works for any bucket of raw probe-cadence
        samples, because the rule operates on "unhealthy sample" counts rather
        than absolute durations.
        """
        if not samples:
            return State.UNKNOWN
        total = len(samples)
        healthy = sum(1 for s in samples if s >= 0.99)
        unhealthy = total - healthy

        # Walk samples to find the longest non-healthy run + its peak state.
        longest_run = 0
        longest_peak = State.HEALTHY
        current_run = 0
        current_peak = State.HEALTHY
        for s in samples:
            if s >= 0.99:
                if current_run > longest_run:
                    longest_run = current_run
                    longest_peak = current_peak
                current_run = 0
                current_peak = State.HEALTHY
                continue
            current_run += 1
            sample_state = State.DEGRADED if s >= 0.5 else State.DOWN
            if sample_state.value > current_peak.value:
                current_peak = sample_state
        if current_run > longest_run:
            longest_run = current_run
            longest_peak = current_peak

        if longest_run >= self.incident_min_consecutive_samples:
            return longest_peak
        if unhealthy <= 1:
            return State.HEALTHY
        return State.DEGRADED

    def _pad_strip(self, cells: list[HourCell], now: float) -> list[HourCell]:
        """Ensure the strip has exactly self.strip_days * 24 hourly entries.

        Missing hours (older than Prometheus retention, or before the probe
        started publishing) are filled with no-data cells so every strip is the
        same length and the bars line up column-for-column across models.
        """
        existing = {c.iso_hour: c for c in cells}
        total_hours = self.strip_days * 24
        current_hour = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        out: list[HourCell] = []
        for i in range(total_hours):
            bucket = current_hour - timedelta(hours=total_hours - 1 - i)
            key = bucket.strftime("%Y-%m-%dT%H:00Z")
            if key in existing:
                out.append(existing[key])
            else:
                out.append(HourCell(iso_hour=key, state=State.UNKNOWN, uptime_fraction=None))
        return out

    async def _detect_incidents(self, now: float) -> list[Incident]:
        """Detect incidents by walking probe-interval success samples.

        Requires `incident_min_consecutive_samples` consecutive non-healthy
        samples to count as an incident. Filters out single-sample blips
        that are typical of KubeRay zero-downtime swaps.
        """
        step_s = self.probe_interval_s
        start = now - (self.recent_history_days * 86400)
        # Aggregate by (model, endpoint) to collapse probe-pod restart series
        # — same reason as _build_hourly_strip. min picks the worst observation
        # at each timestamp, which is what we want for outage detection.
        data = await self.prom.range(
            f'min by (model, endpoint) (openserve_probe_success{{{TIER_FILTER}}})',
            start,
            now,
            step_s,
            timeout_s=INCIDENT_QUERY_TIMEOUT_S,
        )
        if data is None:
            return []

        # Errors: sum already aggregates across instances so restart
        # fragmentation is invisible here. Best-effort.
        errors_data = await self.prom.range(
            f'sum by (model, endpoint, error_type) (increase(openserve_probe_errors_total{{{TIER_FILTER}}}[{step_s}s]))',
            start,
            now,
            step_s,
            timeout_s=INCIDENT_QUERY_TIMEOUT_S,
        ) or []
        errors_by_target = self._index_errors_by_target(errors_data)

        incidents: list[Incident] = []
        min_samples = max(1, self.incident_min_consecutive_samples)
        for entry in data:
            metric = entry.get("metric") or {}
            model = metric.get("model")
            endpoint = metric.get("endpoint")
            if not model or not endpoint:
                continue
            for run_start, run_end, peak, samples in _collapse_runs(entry.get("values") or []):
                if samples < min_samples:
                    continue
                error_type = self._dominant_error_type(
                    errors_by_target.get((model, endpoint), []),
                    run_start,
                    run_end,
                )
                incidents.append(
                    Incident(
                        model_id=model,
                        endpoint=endpoint,
                        start_ts=run_start,
                        end_ts=None if (now - run_end) < step_s else run_end,
                        peak_state=peak,
                        error_type=error_type,
                    )
                )
        incidents.sort(key=lambda i: i.start_ts, reverse=True)
        return incidents

    @staticmethod
    def _index_errors_by_target(
        data: list[dict[str, Any]],
    ) -> dict[tuple[str, str], list[tuple[float, str, float]]]:
        """Returns {(model, endpoint): [(ts, error_type, count), ...]}."""
        out: dict[tuple[str, str], list[tuple[float, str, float]]] = {}
        for entry in data:
            metric = entry.get("metric") or {}
            model = metric.get("model")
            endpoint = metric.get("endpoint")
            error_type = metric.get("error_type")
            if not model or not endpoint or not error_type:
                continue
            for sample in (entry.get("values") or []):
                if len(sample) < 2:
                    continue
                try:
                    ts = float(sample[0])
                    count = float(sample[1])
                except (TypeError, ValueError):
                    continue
                if count <= 0:
                    continue
                out.setdefault((model, endpoint), []).append((ts, error_type, count))
        return out

    @staticmethod
    def _dominant_error_type(
        events: list[tuple[float, str, float]],
        start_ts: float,
        end_ts: float,
    ) -> str | None:
        totals: dict[str, float] = {}
        for ts, error_type, count in events:
            if start_ts <= ts <= end_ts:
                totals[error_type] = totals.get(error_type, 0.0) + count
        if not totals:
            return None
        return max(totals.items(), key=lambda kv: kv[1])[0]

    def _group_history_by_day(self, incidents: list[Incident], now: float) -> list[DailyHistoryEntry]:
        """Build daily cards for the Recent History section.

        Days are emitted newest-first. Each day lists incidents that started
        on that UTC date. Days with no incidents still get a card so the
        timeline is continuous (matches status.openai.com's pattern).
        """
        by_date: dict[str, list[Incident]] = {}
        for inc in incidents:
            by_date.setdefault(inc.start_date, []).append(inc)

        today = datetime.fromtimestamp(now, tz=timezone.utc).date()
        entries: list[DailyHistoryEntry] = []
        for i in range(self.recent_history_days):
            day = today - timedelta(days=i)
            key = day.strftime("%Y-%m-%d")
            entries.append(DailyHistoryEntry(iso_date=key, incidents=by_date.get(key, [])))
        return entries


def _collapse_runs(samples: list[list[Any]]) -> list[tuple[float, float, State, int]]:
    """Find runs of consecutive non-healthy samples.

    Returns (run_start, run_end, peak_state, sample_count) per run.
    """
    runs: list[tuple[float, float, State, int]] = []
    in_run = False
    run_start = 0.0
    run_end = 0.0
    peak = State.HEALTHY
    count = 0
    for sample in samples:
        if len(sample) < 2:
            continue
        try:
            ts = float(sample[0])
            success = float(sample[1])
        except (TypeError, ValueError):
            continue
        if success >= 0.99:
            sample_state = State.HEALTHY
        elif success >= 0.5:
            sample_state = State.DEGRADED
        else:
            sample_state = State.DOWN

        if sample_state == State.HEALTHY:
            if in_run:
                runs.append((run_start, run_end, peak, count))
                in_run = False
                peak = State.HEALTHY
                count = 0
            continue

        if not in_run:
            in_run = True
            run_start = ts
            peak = sample_state
            count = 0
        run_end = ts
        if sample_state > peak:
            peak = sample_state
        count += 1

    if in_run:
        runs.append((run_start, run_end, peak, count))
    return runs


# --- Config loading ---

def _load_json_file(path: str, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to load %s: %s", path, exc)
        return default


def load_model_metadata() -> dict[str, dict[str, str]]:
    raw = _load_json_file(MODEL_METADATA_FILE, {})
    if not isinstance(raw, dict):
        return {}
    return raw


def load_slo_config() -> dict[str, float]:
    raw = _load_json_file(SLO_CONFIG_FILE, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def load_category_order() -> list[str]:
    """Operator-overridable category section order.

    Shape: ["LLM", "Vision-Language", "Embedding", "Reranker", "OCR", ...]
    Categories not in the list are appended alphabetically after.
    """
    raw = _load_json_file(CATEGORY_ORDER_FILE, None)
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return list(DEFAULT_CATEGORY_ORDER)


# --- FastAPI app + Jinja env ---

app = FastAPI(title="open-serve Status")

_template_dir = Path(__file__).parent / "templates"
_static_dir = Path(__file__).parent / "static"

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# Force browsers to revalidate the page and its CSS/JS instead of serving a
# stale heuristically-cached copy. Without this, a new build is masked by the
# browser cache until it happens to expire (the assets carry an ETag but no
# Cache-Control). `no-cache` means "revalidate before use", not "don't store":
# StaticFiles answers the conditional request with a cheap 304 when the ETag is
# unchanged, and a full 200 only when the asset actually changed after a deploy.
_REVALIDATE_PATHS = {"/", "/status", "/status.json"}
_REVALIDATE_PREFIXES = ("/static/",)


@app.middleware("http")
async def _revalidate_user_facing(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in _REVALIDATE_PATHS or path.startswith(_REVALIDATE_PREFIXES):
        response.headers["Cache-Control"] = "no-cache"
    return response

jinja_env = Environment(
    loader=FileSystemLoader(_template_dir),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _format_latency(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    return f"{seconds:.2f}s"


def _format_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_date(iso_day: str) -> str:
    """YYYY-MM-DD → 'May 28, 2026' for human-readable day cards."""
    try:
        d = datetime.strptime(iso_day, "%Y-%m-%d")
    except ValueError:
        return iso_day
    return d.strftime("%b %d, %Y")


def _format_hour(iso_hour: str) -> str:
    """'2026-06-07T14:00Z' → 'Jun 07, 2026 · 14:00 UTC' for hourly-bar tooltips."""
    try:
        d = datetime.strptime(iso_hour, "%Y-%m-%dT%H:00Z")
    except ValueError:
        return iso_hour
    return d.strftime("%b %d, %Y · %H:00 UTC")


def _format_uptime(fraction: float | None) -> str:
    if fraction is None:
        return "—"
    return f"{fraction * 100:.2f}%"


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


jinja_env.filters["latency"] = _format_latency
jinja_env.filters["iso"] = _format_iso
jinja_env.filters["pretty_date"] = _format_date
jinja_env.filters["hour_label"] = _format_hour
jinja_env.filters["uptime"] = _format_uptime
jinja_env.filters["duration"] = _format_duration


# --- Prometheus self-metrics ---

snapshot_build_seconds = Histogram(
    "openserve_status_snapshot_build_seconds",
    "Time taken to build a fresh snapshot (cache miss).",
)
snapshot_cache_hits = Counter(
    "openserve_status_snapshot_cache_hits_total",
    "Cache hits served from the in-memory snapshot cache.",
)
snapshot_cache_misses = Counter(
    "openserve_status_snapshot_cache_misses_total",
    "Cache misses that triggered a fresh snapshot build.",
)
page_renders = Counter(
    "openserve_status_page_renders_total",
    "Pages rendered, by endpoint and content type.",
    ["endpoint", "content_type"],
)


# --- App state ---

@dataclass
class AppState:
    prom: PrometheusClient
    snapshotter: Snapshotter


def build_app_state() -> AppState:
    prom = PrometheusClient(PROMETHEUS_URL)
    metadata = load_model_metadata()
    slo = load_slo_config()
    category_order = load_category_order()
    return AppState(
        prom=prom,
        snapshotter=Snapshotter(prom, metadata, slo, category_order=category_order),
    )


@app.on_event("startup")
async def _startup() -> None:
    app.state.deps = build_app_state()
    logger.info(
        "open-serve status starting: prometheus=%s cache_ttl=%ds strip=%dd history=%dd "
        "min_consecutive_samples=%d probe_interval=%ds",
        PROMETHEUS_URL,
        CACHE_TTL_SECONDS,
        STRIP_DAYS,
        RECENT_HISTORY_DAYS,
        INCIDENT_MIN_CONSECUTIVE_SAMPLES,
        PROBE_INTERVAL_S,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    deps: AppState = app.state.deps
    await deps.prom.close()


# --- Routes ---

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type="text/plain")


def snapshot_to_dict(snap: PageSnapshot) -> dict[str, Any]:
    """Serialize PageSnapshot to /status.json.

    Golden file at status/tests/golden/status.example.json — keep in sync.
    """
    def cell_to_dict(c: HourCell) -> dict[str, Any]:
        return {
            "hour": c.iso_hour,
            "date": c.iso_date,
            "state": c.state.label,
            "color": c.state.color,
            "uptime_fraction": c.uptime_fraction,
            "detail": c.detail,
        }

    def endpoint_to_dict(e: EndpointHealth) -> dict[str, Any]:
        return {
            "endpoint": e.endpoint,
            "state": e.state.label,
            "color": e.state.color,
            "success_rate_5m": e.success_rate_5m,
            "p95_latency_s": e.p95_latency_s,
            "slo_latency_s": e.slo_latency_s,
            "hourly_strip": [cell_to_dict(c) for c in e.hourly_strip],
        }

    def model_to_dict(m: Model) -> dict[str, Any]:
        return {
            "model_id": m.model_id,
            "category": m.category,
            "description": m.description,
            "state": m.state.label,
            "color": m.state.color,
            "uptime_fraction": m.uptime_fraction,
            "endpoints": [endpoint_to_dict(e) for e in m.endpoints],
            "hourly_strip": [cell_to_dict(c) for c in m.hourly_strip],
        }

    def incident_to_dict(inc: Incident) -> dict[str, Any]:
        return {
            "model_id": inc.model_id,
            "endpoint": inc.endpoint,
            "start_ts": inc.start_ts,
            "end_ts": inc.end_ts,
            "peak_state": inc.peak_state.label,
            "duration_s": inc.duration_s,
            "active": inc.is_active,
            "error_type": inc.error_type,
        }

    def history_to_dict(h: DailyHistoryEntry) -> dict[str, Any]:
        return {
            "date": h.iso_date,
            "incidents": [incident_to_dict(i) for i in h.incidents],
        }

    return {
        "rendered_at": snap.rendered_at,
        "data_stale_s": snap.data_stale_s,
        "banner": {
            "state": snap.banner_state.label,
            "color": snap.banner_state.color,
            "fallback_mode": snap.fallback_mode,
        },
        "components": {
            cat: [model_to_dict(m) for m in models]
            for cat, models in snap.models_by_category.items()
        },
        "active_incidents": [incident_to_dict(i) for i in snap.active_incidents],
        "recent_history": [history_to_dict(h) for h in snap.recent_history],
        "strip_days": STRIP_DAYS,
        "strip_hours": STRIP_DAYS * 24,
        "strip_granularity": "hour",
        "recent_history_days": RECENT_HISTORY_DAYS,
        "branding": snap.branding,
    }


async def _get_snapshot() -> PageSnapshot:
    deps: AppState = app.state.deps
    return await deps.snapshotter.get()


@app.get("/status.json")
async def status_json() -> JSONResponse:
    snap = await _get_snapshot()
    page_renders.labels(endpoint="status_json", content_type="application/json").inc()
    return JSONResponse(snapshot_to_dict(snap))


@app.get("/status", response_class=HTMLResponse)
async def status_html(request: Request) -> HTMLResponse:
    snap = await _get_snapshot()
    template = jinja_env.get_template("status.html.jinja")
    # Template uses literal "/status.json" for the JSON link (relative URL
    # avoids the wrong-host issue when the page is served behind the
    # gateway at e.g. models.example.com/status — the status pod's
    # request.url_for() returns an internal cluster URL).
    html = template.render(
        snap=snap,
        State=State,
        strip_days=STRIP_DAYS,
        recent_history_days=RECENT_HISTORY_DAYS,
    )
    page_renders.labels(endpoint="status", content_type="text/html").inc()
    return HTMLResponse(content=html)


@app.get("/")
async def root(request: Request) -> Response:
    snap = await _get_snapshot()
    template = jinja_env.get_template("status.html.jinja")
    html = template.render(
        snap=snap,
        State=State,
        strip_days=STRIP_DAYS,
        recent_history_days=RECENT_HISTORY_DAYS,
    )
    return HTMLResponse(content=html)
