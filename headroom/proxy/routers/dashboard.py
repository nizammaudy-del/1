"""Dashboard-support routes for the Headroom proxy.

Extracted from `headroom.proxy.server` so future dashboard-facing routes can
be added here without touching the giant server module. `build_dashboard_router`
is called from `create_app()` and closes over the same request-scoped
`proxy`/`config` objects the routes previously captured as nested functions.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from headroom.agent_savings import proxy_pipeline_kwargs
from headroom.dashboard import get_dashboard_html
from headroom.observability import get_langfuse_tracing_status, get_otel_metrics_status
from headroom.proxy import server as _server
from headroom.proxy import user_config
from headroom.proxy.cost import build_prefix_cache_stats, build_session_summary, merge_cost_stats
from headroom.proxy.helpers import initialize_context_tool_session_baseline
from headroom.proxy.loopback_guard import require_loopback as _require_loopback
from headroom.proxy.modes import PROXY_MODE_TOKEN
from headroom.proxy.savings_tracker import LITELLM_AVAILABLE
from headroom.proxy.server import (
    HeadroomProxy,
    ProxyConfig,
    _build_agent_usage_summary,
    _classify_agent_from_log,
    _request_is_loopback,
    logger,
)
from headroom.subscription.base import get_quota_registry


def build_dashboard_router(proxy: HeadroomProxy, config: ProxyConfig) -> APIRouter:
    """Build the router for dashboard-support endpoints.

    `proxy` and `config` are the same objects `create_app()` builds for the
    rest of the app; the nested routes below close over them exactly like
    they did as inline `@app.get`/`@app.post` handlers in server.py.
    """
    router = APIRouter()

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        """Serve the Headroom dashboard UI."""
        return get_dashboard_html()

    @router.get("/favicon.ico")
    async def favicon() -> Response:
        # Registered before register_provider_routes' catch-all passthrough
        # route so browsers' automatic favicon requests for /dashboard are
        # answered locally instead of being tunneled to the wrapped upstream
        # provider (GH #1787).
        return Response(status_code=204)

    DASHBOARD_STATS_CACHE_TTL_SECONDS = 5.0
    _stats_snapshot_lock = asyncio.Lock()
    _stats_snapshot: dict[str, Any] = {"expires_at": 0.0, "value": None}

    THROUGHPUT_CACHE_TTL_SECONDS = 10.0
    _throughput_cache_lock = asyncio.Lock()
    _throughput_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}

    RECENT_REQUEST_LOG_WINDOW = 100

    RECENT_REQUEST_RENDER_DEFAULT = 10
    RECENT_REQUEST_RENDER_MAX = 50

    def _build_recent_request_payload(
        limit: int = RECENT_REQUEST_LOG_WINDOW,
        render_limit: int = RECENT_REQUEST_RENDER_DEFAULT,
    ) -> dict[str, Any]:
        # render_limit controls how many of the already-fetched recent logs are
        # rendered to the dashboard (the "Show more" pagination). It never
        # exceeds RECENT_REQUEST_LOG_WINDOW since that's all `limit` fetches.
        render_limit = max(1, min(render_limit, RECENT_REQUEST_RENDER_MAX, limit))
        recent_request_logs = proxy.logger.get_recent(limit) if proxy.logger else []
        dashboard_recent_requests = [
            {
                "request_id": log.get("request_id"),
                "timestamp": log.get("timestamp"),
                "provider": log.get("provider"),
                "model": log.get("model"),
                "input_tokens_original": log.get("input_tokens_original"),
                "input_tokens_optimized": log.get("input_tokens_optimized"),
                "output_tokens": log.get("output_tokens"),
                "tokens_saved": log.get("tokens_saved"),
                "savings_percent": log.get("savings_percent"),
                "optimization_latency_ms": log.get("optimization_latency_ms"),
                "total_latency_ms": log.get("total_latency_ms"),
                "transforms_applied": log.get("transforms_applied", []),
                "waste_signals": log.get("waste_signals"),
                "tool_schema_saved_tokens": _server._tool_schema_saved_from_tags(log.get("tags")),
            }
            for log in recent_request_logs
            if log.get("input_tokens_original") is not None
            and log.get("input_tokens_optimized") is not None
        ][-render_limit:]
        return {
            "request_logs": recent_request_logs[-render_limit:],
            "recent_requests": dashboard_recent_requests,
        }

    async def _build_stats_payload() -> dict[str, Any]:
        """Build the full `/stats` response payload.

        This is the main stats endpoint - it aggregates data from all subsystems:
        - Request metrics (total, cached, failed, by model/provider)
        - Token usage and savings
        - Cost tracking
        - Canonical persisted display_session metrics for downstream dashboards
        - Compression (CCR) statistics
        - Telemetry/TOIN (data flywheel) statistics
        - Cache and rate limiter stats
        """
        m = proxy.metrics

        async with _throughput_cache_lock:
            now = time.time()
            if _throughput_cache["expires_at"] < now or _throughput_cache["value"] is None:

                def _compute_throughput() -> Any:
                    from headroom.perf.analyzer import build_perf_summary, parse_log_files

                    perf_report = parse_log_files(last_n_hours=1.0)
                    return build_perf_summary(perf_report).get("throughput")

                try:
                    throughput = await asyncio.to_thread(_compute_throughput)
                    _throughput_cache["value"] = throughput
                    _throughput_cache["expires_at"] = now + THROUGHPUT_CACHE_TTL_SECONDS
                except Exception as e:
                    logger.warning("Failed to calculate throughput for stats: %s", e, exc_info=True)
                    if _throughput_cache["value"] is None:
                        _throughput_cache["value"] = None
            throughput = _throughput_cache["value"]

        # Calculate average latency
        avg_latency_ms = round(m.latency_sum_ms / m.latency_count, 2) if m.latency_count > 0 else 0
        min_latency_ms = (
            round(m.latency_min_ms, 2)
            if m.latency_count > 0 and m.latency_min_ms != float("inf")
            else 0
        )
        max_latency_ms = round(m.latency_max_ms, 2) if m.latency_count > 0 else 0

        # Calculate Headroom overhead (optimization time only, excludes pass-through requests)
        avg_overhead_ms = (
            round(m.overhead_sum_ms / m.overhead_count, 2) if m.overhead_count > 0 else 0
        )
        min_overhead_ms = (
            round(m.overhead_min_ms, 2)
            if m.overhead_count > 0 and m.overhead_min_ms != float("inf")
            else 0
        )
        max_overhead_ms = round(m.overhead_max_ms, 2) if m.overhead_count > 0 else 0

        # Calculate TTFB (time to first byte)
        avg_ttfb_ms = round(m.ttfb_sum_ms / m.ttfb_count, 2) if m.ttfb_count > 0 else 0
        min_ttfb_ms = (
            round(m.ttfb_min_ms, 2) if m.ttfb_count > 0 and m.ttfb_min_ms != float("inf") else 0
        )
        max_ttfb_ms = round(m.ttfb_max_ms, 2) if m.ttfb_count > 0 else 0

        def _pct(part: int | float, whole: int | float) -> float:
            return round((float(part) / float(whole)) * 100.0, 2) if whole else 0.0

        # Get compression store stats
        store = _server.get_compression_store()
        compression_stats = store.get_stats()

        # Get telemetry/TOIN stats
        telemetry = _server.get_telemetry_collector()
        telemetry_stats = telemetry.get_stats()

        # Get feedback loop stats
        feedback = _server.get_compression_feedback()
        feedback_stats = feedback.get_stats()

        # Build prefix cache stats once (used in both prefix_cache and cost)
        prefix_cache_stats = build_prefix_cache_stats(m, proxy.cost_tracker)

        # Fetch CLI filtering savings from the selected context tool. These
        # tokens are avoided before they reach model context.
        cli_filtering_stats = await asyncio.to_thread(_server._get_context_tool_stats)
        cli_filtering_tool = (
            str(cli_filtering_stats.get("tool", "rtk")) if cli_filtering_stats else "rtk"
        )
        cli_filtering_label = (
            str(cli_filtering_stats.get("label", "RTK")) if cli_filtering_stats else "RTK"
        )
        cli_tokens_avoided = (
            cli_filtering_stats.get("tokens_saved", 0) if cli_filtering_stats else 0
        )
        cli_filtering_session = (
            cli_filtering_stats.get("session", {}) if cli_filtering_stats else {}
        )
        cli_filtering_lifetime = (
            cli_filtering_stats.get("lifetime", {}) if cli_filtering_stats else {}
        )
        rtk_tokens_avoided = cli_tokens_avoided if cli_filtering_tool == "rtk" else 0
        lean_ctx_tokens_avoided = cli_tokens_avoided if cli_filtering_tool == "lean-ctx" else 0
        cli_filtering_available = bool(
            cli_filtering_stats and cli_filtering_stats.get("installed", False)
        )

        # Calculate total tokens before Headroom-side reduction. Proxy
        # compression and the configured context tool both remove tokens before
        # they reach model context, so dashboard-facing savings combines them.
        proxy_compression_tokens = m.tokens_saved_total
        all_layers_tokens_saved = proxy_compression_tokens + cli_tokens_avoided
        total_tokens_before = m.tokens_input_total + all_layers_tokens_saved
        proxy_total_before_compression = m.tokens_input_total + proxy_compression_tokens
        # `attempted_input_tokens` is the compressible-only denominator
        # (extracted units + tool schema). The "active compression"
        # ratio is what fraction of the tokens we *tried* to compress
        # actually got compressed. Excludes prefix-frozen content
        # (user/system messages, prior turns) we never touched —
        # otherwise the ratio is dominated by content we deliberately
        # avoided changing for prefix-cache safety.
        # `attempted_input_tokens_total` is already pre-compression: it
        # accumulates `unit.tokens_before` for each eligible unit that
        # reached the router, plus the original (pre-compaction) tool
        # schema size. So the savings rate is plain `saved / attempted`
        # — adding `saved` again would double-count.
        attempted_input_tokens = getattr(m, "attempted_input_tokens_total", 0)

        # Build human-readable summary
        summary = build_session_summary(
            proxy, m, prefix_cache_stats, cli_tokens_avoided, total_tokens_before
        )
        # DEBUG: log the summary payload for external upsert consumers
        try:
            logger.debug("/stats summary data: %r", summary)
        except Exception:
            logger.warning("Failed to log /stats summary payload")

        # Compression cache stats (token mode). Snapshot the cache list under
        # the dict lock so a concurrent eviction can't mutate the dict while
        # we iterate. Each per-session `get_stats()` is independently
        # thread-safe via the cache's own internal lock.
        compression_cache_stats: dict = {}
        if proxy.config.mode == PROXY_MODE_TOKEN and proxy._compression_caches:
            with proxy._compression_caches_lock:
                _caches_snapshot = list(proxy._compression_caches.values())
                _active_sessions = len(proxy._compression_caches)
            total_entries = 0
            total_hits = 0
            total_misses = 0
            total_tokens_saved = 0
            for cache in _caches_snapshot:
                s = cache.get_stats()
                total_entries += s.get("entries", 0)
                total_hits += s.get("hits", 0)
                total_misses += s.get("misses", 0)
                total_tokens_saved += s.get("total_tokens_saved", 0)
            compression_cache_stats = {
                "mode": PROXY_MODE_TOKEN,
                "active_sessions": _active_sessions,
                "total_entries": total_entries,
                "total_hits": total_hits,
                "total_misses": total_misses,
                "hit_rate": round(total_hits / max(1, total_hits + total_misses) * 100, 1),
                "total_tokens_saved": total_tokens_saved,
            }
        else:
            compression_cache_stats = {"mode": proxy.config.mode}

        # Build unified savings summary (all layers)
        cache_net_usd = prefix_cache_stats.get("totals", {}).get("net_savings_usd", 0.0)
        total_tokens_all_layers = all_layers_tokens_saved
        persistent_savings = m.savings_tracker.stats_preview()
        display_session = persistent_savings.get("display_session", {})
        recent_request_logs = proxy.logger.get_recent(10_000) if proxy.logger else []
        recent_request_payload = _build_recent_request_payload()

        # Tool-schema deferral savings: tool-definition tokens kept out of the
        # model's context by deferring heavy schemas until they're needed
        # (native tool-search injection + any registered turn-hook tools
        # rewrite). Attributed to Headroom only — see _tool_schema_saved_from_tags.
        # Aggregated over the recent request-log window.
        tool_schema_tokens = 0
        tool_schema_requests = 0
        for _ts_log in recent_request_logs:
            _ts_saved = _server._tool_schema_saved_from_tags(_ts_log.get("tags"))
            if _ts_saved > 0:
                tool_schema_tokens += _ts_saved
                tool_schema_requests += 1
        agent_usage = _build_agent_usage_summary(
            recent_request_logs,
            requests_by_provider=dict(m.requests_by_provider),
            requests_by_model=dict(m.requests_by_model),
            global_before_tokens=proxy_total_before_compression,
            global_after_tokens=m.tokens_input_total,
            global_tokens_saved=proxy_compression_tokens,
            global_output_tokens=m.tokens_output_total,
        )

        # Output-side reduction (counterfactual estimate from the shaper's
        # ledger). Distinct from input compression above: these are OUTPUT
        # tokens the model didn't emit because we steered verbosity / routed
        # effort down. Always labelled estimated-vs-measured + a CI so it's
        # never mistaken for an exact count. Best-effort — never break /stats.
        output_reduction: dict[str, Any] = {"available": False}
        try:
            from headroom.proxy.output_savings import get_recorder

            _oest = get_recorder().estimate()
            if _oest.n_requests > 0:
                output_reduction = {
                    "available": True,
                    "method": _oest.kind,  # "measured" | "estimated"
                    "tokens_saved": round(_oest.tokens_saved),
                    "baseline_tokens": round(_oest.baseline_tokens),
                    "reduction_percent": round(_oest.pct, 1),
                    "ci_low_percent": round(_oest.ci_low_pct, 1),
                    "ci_high_percent": round(_oest.ci_high_pct, 1),
                    "requests": _oest.n_requests,
                }
        except Exception:  # pragma: no cover - defensive
            pass

        return {
            "summary": summary,
            "agent_usage": agent_usage,
            "savings": {
                "total_tokens": total_tokens_all_layers,
                "per_project": persistent_savings.get("projects", {}),
                "by_layer": {
                    "cli_filtering": {
                        "tool": cli_filtering_tool,
                        "label": cli_filtering_label,
                        "available": cli_filtering_available,
                        "tokens": cli_tokens_avoided,
                        "tokens_saved": cli_tokens_avoided,
                        "session": cli_filtering_session,
                        "lifetime": cli_filtering_lifetime,
                        "session_savings_pct": (
                            cli_filtering_stats.get("session_savings_pct")
                            if cli_filtering_stats
                            else None
                        ),
                        "lifetime_savings_pct": (
                            cli_filtering_stats.get("lifetime_avg_savings_pct")
                            if cli_filtering_stats
                            else None
                        ),
                        "refresh_interval_seconds": (
                            cli_filtering_stats.get("refresh_interval_seconds")
                            if cli_filtering_stats
                            else None
                        ),
                        "included_in": "tokens.saved",
                        "description": (
                            f"Tokens avoided by CLI output filtering ({cli_filtering_label}) "
                            "before reaching context. "
                            "Included in dashboard token savings, but not in dollar savings."
                        ),
                    },
                    "compression": {
                        "tokens": proxy_compression_tokens,
                        "proxy_tokens": proxy_compression_tokens,
                        "cli_filtering_tokens": cli_tokens_avoided,
                        "rtk_tokens": rtk_tokens_avoided,
                        "lean_ctx_tokens": lean_ctx_tokens_avoided,
                        "all_layers_tokens": all_layers_tokens_saved,
                        "description": (
                            "Tokens removed by Headroom proxy compression. "
                            "Dashboard token savings also includes CLI context-tool filtering."
                        ),
                    },
                    "prefix_cache": {
                        "discount_usd": round(cache_net_usd, 4),
                        "description": (
                            "Cost discount from provider prefix caching. "
                            "Headroom's CacheAligner improves hit rates; "
                            "baseline caching is provider-native."
                        ),
                    },
                    "output_shaping": {
                        **output_reduction,
                        "description": (
                            "OUTPUT tokens the model didn't emit because the shaper "
                            "steered verbosity / routed effort down. Counterfactual — "
                            "shown as an estimate (vs a learned baseline) or measured "
                            "(A/B holdout), always with a confidence band."
                        ),
                    },
                    "tool_search": {
                        "tokens": tool_schema_tokens,
                        "tokens_saved": tool_schema_tokens,
                        "requests": tool_schema_requests,
                        "window": len(recent_request_logs),
                        "description": (
                            "Tool-definition tokens kept out of the model's context "
                            "by deferring heavy tool schemas until they're searched "
                            "for. Counted only when Headroom performed the deferral — "
                            "not when the client (e.g. Claude Code / Codex) already "
                            "had tool search enabled. Aggregated over the recent "
                            "request window."
                        ),
                    },
                },
            },
            "requests": {
                "total": m.requests_total,
                "cached": m.requests_cached,
                "rate_limited": m.requests_rate_limited,
                "failed": m.requests_failed,
                "by_provider": dict(m.requests_by_provider),
                "by_model": dict(m.requests_by_model),
                "by_stack": dict(m.requests_by_stack),
            },
            "tokens": {
                "input": m.tokens_input_total,
                "output": m.tokens_output_total,
                "output_saved": output_reduction.get("tokens_saved", 0),
                "output_reduction_percent": output_reduction.get("reduction_percent", 0),
                "output_reduction": output_reduction,
                "saved": all_layers_tokens_saved,
                "proxy_compression_saved": proxy_compression_tokens,
                "cli_filtering_saved": cli_tokens_avoided,
                "rtk_saved": rtk_tokens_avoided,
                "lean_ctx_saved": lean_ctx_tokens_avoided,
                "cli_tokens_avoided": cli_tokens_avoided,
                "proxy_total_before_compression": proxy_total_before_compression,
                "total_before_compression": total_tokens_before,
                "all_layers_saved": all_layers_tokens_saved,
                # Compressible-only denominator: tokens we extracted as
                # candidates + tool-schema tokens we compacted. Excludes
                # frozen-prefix content (user msgs, system prompt, prior
                # turns) that we deliberately don't touch. Already
                # pre-compression — do NOT add `tokens_saved` again.
                "proxy_attempted_tokens": attempted_input_tokens,
                # Active compression: savings as a fraction of what we
                # *tried* to compress. The number the dashboard headline
                # should show — it answers "are we doing well *when we
                # have something to compress?*" rather than diluting the
                # win by frozen-prefix bytes we never touched.
                "active_savings_percent": round(
                    (proxy_compression_tokens / attempted_input_tokens * 100)
                    if attempted_input_tokens > 0
                    else 0,
                    2,
                ),
                # Whole-request ratio kept for transparency. Heavily
                # diluted by frozen prefix on Codex-style requests
                # where most input is non-compressible by design.
                "proxy_savings_percent": round(
                    (proxy_compression_tokens / proxy_total_before_compression * 100)
                    if proxy_total_before_compression > 0
                    else 0,
                    2,
                ),
                "savings_percent": round(
                    (all_layers_tokens_saved / total_tokens_before * 100)
                    if total_tokens_before > 0
                    else 0,
                    2,
                ),
                "all_layers_savings_percent": round(
                    (all_layers_tokens_saved / total_tokens_before * 100)
                    if total_tokens_before > 0
                    else 0,
                    2,
                ),
            },
            "latency": {
                "average_ms": avg_latency_ms,
                "min_ms": min_latency_ms,
                "max_ms": max_latency_ms,
                "total_requests": m.latency_count,
            },
            "overhead": {
                "average_ms": avg_overhead_ms,
                "min_ms": min_overhead_ms,
                "max_ms": max_overhead_ms,
            },
            "ttfb": {
                "average_ms": avg_ttfb_ms,
                "min_ms": min_ttfb_ms,
                "max_ms": max_ttfb_ms,
            },
            "pipeline_timing": {
                name: {
                    "average_ms": round(
                        m.transform_timing_sum[name] / m.transform_timing_count[name], 2
                    ),
                    "max_ms": round(m.transform_timing_max[name], 2),
                    "count": m.transform_timing_count[name],
                }
                for name in sorted(m.transform_timing_sum.keys())
            }
            if m.transform_timing_sum
            else {},
            "compressions_by_strategy": dict(m.compressions_by_strategy),
            "tokens_saved_by_strategy": dict(m.tokens_saved_by_strategy),
            "codex_ws": {
                "units_total": m.codex_ws_units_total,
                "units_modified_total": m.codex_ws_units_modified_total,
                "units_by_strategy": dict(m.codex_ws_units_by_strategy),
                "units_by_category": dict(m.codex_ws_units_by_category),
                "units_by_content_type": dict(m.codex_ws_units_by_content_type),
                "units_by_text_shape": dict(m.codex_ws_units_by_text_shape),
                "units_to_kompress_total": m.codex_ws_units_to_kompress_total,
                "units_kompress_attempted_total": m.codex_ws_units_kompress_attempted_total,
                "units_to_kompress_percent": _pct(
                    m.codex_ws_units_to_kompress_total,
                    m.codex_ws_units_total,
                ),
                "units_kompress_attempted_percent": _pct(
                    m.codex_ws_units_kompress_attempted_total,
                    m.codex_ws_units_total,
                ),
                "unit_elapsed_ms": {
                    "average": round(
                        m.codex_ws_unit_elapsed_ms_sum / m.codex_ws_units_total,
                        2,
                    )
                    if m.codex_ws_units_total
                    else 0.0,
                    "max": round(m.codex_ws_unit_elapsed_ms_max, 2),
                },
                "unit_bytes_sum": m.codex_ws_unit_bytes_sum,
                "unit_tokens_before_sum": m.codex_ws_unit_tokens_before_sum,
                "unit_tokens_after_sum": m.codex_ws_unit_tokens_after_sum,
                "unit_tokens_saved_sum": m.codex_ws_unit_tokens_saved_sum,
                "frames_attempted_total": m.codex_ws_frames_attempted_total,
                "frames_compressed_total": m.codex_ws_frames_compressed_total,
                "frames_failed_total": m.codex_ws_frames_failed_total,
                "frames_to_kompress_total": m.codex_ws_frames_to_kompress_total,
                "frames_kompress_attempted_total": (m.codex_ws_frames_kompress_attempted_total),
                "frames_to_kompress_percent": _pct(
                    m.codex_ws_frames_to_kompress_total,
                    m.codex_ws_frames_attempted_total,
                ),
                "frames_kompress_attempted_percent": _pct(
                    m.codex_ws_frames_kompress_attempted_total,
                    m.codex_ws_frames_attempted_total,
                ),
                "frame_elapsed_ms": {
                    "average": round(
                        m.codex_ws_frame_elapsed_ms_sum / m.codex_ws_frames_attempted_total,
                        2,
                    )
                    if m.codex_ws_frames_attempted_total
                    else 0.0,
                    "max": round(m.codex_ws_frame_elapsed_ms_max, 2),
                },
                "frame_bytes_before_sum": m.codex_ws_frame_bytes_before_sum,
                "frame_bytes_after_sum": m.codex_ws_frame_bytes_after_sum,
                "frame_attempted_tokens_sum": m.codex_ws_frame_attempted_tokens_sum,
                "frame_tokens_saved_sum": m.codex_ws_frame_tokens_saved_sum,
            },
            "waste_signals": dict(m.waste_signals_total) if m.waste_signals_total else {},
            # ContentRouter protection categories aggregated across the
            # session. Lets operators see, e.g., that 80% of messages
            # were `user_msg` (protected) and only 5% reached the
            # compressor — explains why compression rate is low and
            # whether `--compress-user-messages` would help (#454).
            "router": {
                "route_counts": dict(m.router_route_counts) if m.router_route_counts else {},
            },
            "savings_history": m.savings_history[-100:],  # Last 100 data points
            "display_session": display_session,
            # Whether LiteLLM is importable. Pricing (the "$ Saved" tile) is
            # derived entirely from LiteLLM's cost tables, and LiteLLM is gated
            # off on Python >=3.14 in pyproject — so when this is False the
            # dashboard tells the user to reinstall on 3.13 instead of just
            # showing $0.00 forever.
            "litellm_available": LITELLM_AVAILABLE,
            "persistent_savings": persistent_savings,
            "prefix_cache": prefix_cache_stats,
            "cost": merge_cost_stats(
                proxy.cost_tracker.stats() if proxy.cost_tracker else None,
                prefix_cache_stats,
                cli_tokens_avoided=cli_tokens_avoided,
            ),
            "compression": {
                "ccr_entries": compression_stats.get("entry_count", 0),
                "ccr_max_entries": compression_stats.get("max_entries", 0),
                "original_tokens_cached": compression_stats.get("total_original_tokens", 0),
                "compressed_tokens_cached": compression_stats.get("total_compressed_tokens", 0),
                "ccr_retrievals": compression_stats.get("total_retrievals", 0),
            },
            "compression_cache": compression_cache_stats,
            # Always False: the anonymous telemetry beacon was removed, so no
            # telemetry is ever shipped externally (local collection only).
            "anon_telemetry_shipping": False,
            "telemetry": {
                "enabled": telemetry_stats.get("enabled", False),
                "total_compressions": telemetry_stats.get("total_compressions", 0),
                "total_retrievals": telemetry_stats.get("total_retrievals", 0),
                "global_retrieval_rate": round(telemetry_stats.get("global_retrieval_rate", 0), 4),
                "tool_signatures_tracked": telemetry_stats.get("tool_signatures_tracked", 0),
                "avg_compression_ratio": round(telemetry_stats.get("avg_compression_ratio", 0), 4),
                "avg_token_reduction": round(telemetry_stats.get("avg_token_reduction", 0), 4),
            },
            "otel": get_otel_metrics_status(),
            "langfuse": get_langfuse_tracing_status(),
            "feedback_loop": {
                "tools_tracked": feedback_stats.get("tools_tracked", 0),
                "total_compressions": feedback_stats.get("total_compressions", 0),
                "total_retrievals": feedback_stats.get("total_retrievals", 0),
                "global_retrieval_rate": round(feedback_stats.get("global_retrieval_rate", 0), 4),
                "tools_with_high_retrieval": sum(
                    1
                    for p in feedback_stats.get("tool_patterns", {}).values()
                    if p.get("retrieval_rate", 0) > 0.3
                ),
            },
            "toin": _server.get_toin().get_stats(),
            "context_tool": {
                "configured": cli_filtering_tool,
                "label": cli_filtering_label,
                "available": bool(
                    cli_filtering_stats and cli_filtering_stats.get("installed", False)
                ),
                "stats": cli_filtering_stats,
            },
            "cli_filtering": cli_filtering_stats,
            "proxy_inbound": proxy.metrics.inbound_snapshot(),
            "cache": await proxy.cache.stats() if proxy.cache else None,
            "rate_limiter": await proxy.rate_limiter.stats() if proxy.rate_limiter else None,
            **recent_request_payload,
            "log_full_messages": proxy.config.log_full_messages if proxy else False,
            **get_quota_registry().get_all_stats(),
            "throughput": throughput,
        }

    def _dashboard_config_payload() -> dict[str, Any]:
        profile_kwargs = proxy_pipeline_kwargs(config)
        target_ratio = profile_kwargs.get("target_ratio", config.target_ratio)
        target_savings_percent = None
        if isinstance(target_ratio, int | float):
            target_savings_percent = round(max(0.0, min(1.0, 1.0 - float(target_ratio))) * 100, 1)
        return {
            "savings_profile": config.savings_profile,
            "target_ratio": target_ratio,
            "target_savings_percent": target_savings_percent,
            "compress_user_messages": bool(
                profile_kwargs.get("compress_user_messages", config.compress_user_messages)
            ),
            "compress_system_messages": bool(
                profile_kwargs.get("compress_system_messages", config.compress_system_messages)
            ),
            "protect_recent": profile_kwargs.get("read_protection_window", config.protect_recent),
            "protect_analysis_context": config.protect_analysis_context,
            "min_tokens_to_crush": profile_kwargs.get(
                "min_tokens_to_compress", config.min_tokens_to_crush
            ),
            "max_items_after_crush": profile_kwargs.get(
                "max_items_after_crush", config.max_items_after_crush
            ),
            "smart_crusher_with_compaction": profile_kwargs.get(
                "smart_crusher_with_compaction",
                config.smart_crusher_with_compaction,
            ),
            "force_kompress": bool(profile_kwargs.get("force_kompress", False)),
            "accuracy_guard": config.accuracy_guard,
        }

    async def _get_cached_stats_payload() -> dict[str, Any]:
        """Return a short-TTL cached `/stats` snapshot for dashboard polling."""
        now = time.monotonic()
        cached_payload = cast(dict[str, Any] | None, _stats_snapshot.get("value"))
        if cached_payload is not None and now < float(_stats_snapshot["expires_at"]):
            return cached_payload

        async with _stats_snapshot_lock:
            now = time.monotonic()
            cached_payload = cast(dict[str, Any] | None, _stats_snapshot.get("value"))
            if cached_payload is not None and now < float(_stats_snapshot["expires_at"]):
                return cached_payload

            payload = await _build_stats_payload()
            _stats_snapshot["value"] = payload
            _stats_snapshot["expires_at"] = time.monotonic() + DASHBOARD_STATS_CACHE_TTL_SECONDS
            return payload

    @router.get("/stats")
    async def stats(
        request: Request, cached: bool = False, recent_limit: int = RECENT_REQUEST_RENDER_DEFAULT
    ) -> dict[str, Any]:
        """Get comprehensive proxy statistics.

        This is the main stats endpoint - it aggregates data from all subsystems:
        - Request metrics (total, cached, failed, by model/provider)
        - Token usage and savings
        - Cost tracking
        - Canonical persisted display_session metrics for downstream dashboards
        - Compression (CCR) statistics
        - Telemetry/TOIN (data flywheel) statistics
        - Cache and rate limiter stats

        Use ``?cached=1`` for the dashboard fast path. That returns a short-TTL
        snapshot to avoid rebuilding the full payload on every UI poll.

        ``recent_limit`` (default 10, capped at 50) controls how many of the
        recent request logs are rendered in ``recent_requests``/``request_logs``
        — used by the dashboard's "Show more" pagination.

        ``recent_requests`` / ``request_logs`` (per-request ids, providers,
        models, errors) and ``config`` (backend + savings profile) are embedded
        only for loopback callers — the local dashboard. Network callers still
        get the aggregate counters but never the per-request metadata.
        """
        include_sensitive = _request_is_loopback(request)
        if cached:
            payload = dict(await _get_cached_stats_payload())
            if include_sensitive:
                # Refresh the per-request tail on top of the cached snapshot.
                payload.update(_build_recent_request_payload(render_limit=recent_limit))
                payload["config"] = _dashboard_config_payload()
        else:
            payload = await _build_stats_payload()
            if include_sensitive:
                # _build_stats_payload already baked in the recent tail at the
                # default limit; only rebuild (re-reading the logger) when a
                # non-default limit was requested.
                if recent_limit != RECENT_REQUEST_RENDER_DEFAULT:
                    payload.update(_build_recent_request_payload(render_limit=recent_limit))
                payload["config"] = _dashboard_config_payload()
        if not include_sensitive:
            # _build_stats_payload bakes these in; strip for network callers.
            payload.pop("recent_requests", None)
            payload.pop("request_logs", None)
        return payload

    @router.post("/stats/reset", dependencies=[Depends(_require_loopback)])
    async def stats_reset() -> JSONResponse:
        """Reset in-memory proxy stats for local test/debug isolation."""
        await proxy.metrics.reset_runtime()
        if proxy.cost_tracker:
            proxy.cost_tracker.reset_runtime()
        await initialize_context_tool_session_baseline()
        async with _stats_snapshot_lock:
            _stats_snapshot["value"] = None
            _stats_snapshot["expires_at"] = 0.0
        return JSONResponse(status_code=200, content={"status": "reset"})

    @router.get("/stats-history", response_model=None)
    async def stats_history(
        format: Literal["json", "csv"] = "json",
        series: Literal["history", "hourly", "daily", "weekly", "monthly"] = "history",
        history_mode: Literal["compact", "full", "none"] = "compact",
    ) -> Response | dict[str, Any]:
        """Get durable proxy compression history plus display-session state.

        The JSON payload also carries a ``cli_filtering`` key with live RTK
        stats. This is a curated subset (``tool``, ``label``, ``available``,
        ``lifetime``, ``session``) tailored to the Historical tab, not the
        full ``_get_context_tool_stats()`` payload that ``/stats`` exposes.
        It is ``None`` only when the stats read hard-fails; when the tool is
        merely absent, ``cli_filtering`` stays populated with
        ``available: False`` and zeroed counters so the tab can distinguish
        "not installed" from "installed, no data yet."
        """
        if format == "csv":
            filename = f"headroom-stats-history-{series}.csv"
            return Response(
                content=proxy.metrics.savings_tracker.export_csv(series=series),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        history = proxy.metrics.savings_tracker.history_response(history_mode=history_mode)

        # Augment with live RTK/cli-filtering lifetime stats so the Historical
        # tab can display them.  These live in the context-tool's own stats file
        # and survive proxy restarts — exactly what the Historical tab needs.
        # Best-effort: if the RTK stats file can't be read (missing, parse error,
        # IO), fall back to None so the Historical tab stays available instead of
        # 500ing. The tab hides the card when cli_filtering is None.
        try:
            cli_stats = await asyncio.to_thread(_server._get_context_tool_stats)
        except Exception:
            logger.debug("stats-history: RTK stats unavailable", exc_info=True)
            cli_stats = None
        if cli_stats:
            history["cli_filtering"] = {
                "tool": str(cli_stats.get("tool", "rtk")),
                "label": str(cli_stats.get("label", "RTK")),
                "available": bool(cli_stats.get("installed", False)),
                "lifetime": cli_stats.get("lifetime", {}),
                "session": cli_stats.get("session", {}),
            }
        else:
            history["cli_filtering"] = None

        return history

    @router.get("/mcp/dashboards", dependencies=[Depends(_require_loopback)])
    async def mcp_dashboards() -> JSONResponse:
        """List installed MCP servers that expose their own web dashboard.

        Only servers we know ship a dashboard are considered; for each we check
        whether it is registered in any detected agent and probe reachability so
        the UI can render live links.
        """
        # Known MCP web dashboards keyed by the server name used at registration.
        known = {
            "serena": {
                "label": "Serena",
                "url": "http://127.0.0.1:24282/dashboard/index.html",
            },
        }

        def _collect() -> list[dict[str, Any]]:
            from headroom.mcp_registry import get_all_registrars

            registrars = get_all_registrars()
            out: list[dict[str, Any]] = []
            for name, meta in known.items():
                installed = any(r.detect() and r.get_server(name) is not None for r in registrars)
                if not installed:
                    continue
                available = False
                try:
                    resp = httpx.get(meta["url"], timeout=0.7)
                    available = resp.status_code < 500
                except httpx.HTTPError:
                    available = False
                out.append(
                    {
                        "name": name,
                        "label": meta["label"],
                        "url": meta["url"],
                        "available": available,
                    }
                )
            return out

        return JSONResponse(content={"dashboards": await asyncio.to_thread(_collect)})

    # ponytail: the proxy does not track a dedicated per-MCP-server counter, so
    # this aggregates mcp__<server>__<tool> tool_use blocks out of the recent
    # request message logs instead of a real-time usage table.
    @router.get("/mcp/usage", dependencies=[Depends(_require_loopback)])
    async def mcp_usage(limit: int = 100) -> JSONResponse:
        """Per-MCP tool-call usage aggregated from recently logged requests.

        Reflects only requests captured while ``log_full_messages`` is enabled,
        bounded by ``limit``. Token/cost are not attributable to a single tool
        within a multi-tool request, so only call counts are reported here —
        deliberately not fabricated.
        """
        limit = max(1, min(limit, 100))

        log_full_messages = bool(proxy.config.log_full_messages) if proxy else False
        by_server: dict[str, dict[str, Any]] = {}

        if proxy and proxy.logger:
            for log in proxy.logger.get_recent_with_messages(limit):
                messages = log.get("request_messages") or []
                for message in messages:
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = str(block.get("name") or "")
                        if not name.startswith("mcp__"):
                            continue
                        parts = name.split("__")
                        server = parts[1] if len(parts) > 1 else "unknown"
                        tool = "__".join(parts[2:]) if len(parts) > 2 else name
                        tool = tool or name
                        entry = by_server.setdefault(
                            server, {"server": server, "calls": 0, "tools": {}}
                        )
                        entry["calls"] += 1
                        entry["tools"][tool] = entry["tools"].get(tool, 0) + 1

        servers = sorted(by_server.values(), key=lambda s: s["calls"], reverse=True)
        for s in servers:
            s["tools"] = [
                {"tool": t, "calls": c}
                for t, c in sorted(s["tools"].items(), key=lambda kv: kv[1], reverse=True)
            ]

        return JSONResponse(
            content={
                "servers": servers,
                "log_full_messages": log_full_messages,
                "sampled_requests": limit,
                "note": (
                    "Call counts from mcp__ tool_use blocks in recently logged "
                    "requests. Requires --log-messages; token/cost are not "
                    "attributable per tool."
                ),
            }
        )

    @router.get("/v1/retrieve/stats", dependencies=[Depends(_require_loopback)])
    async def ccr_stats() -> dict[str, Any]:
        """Get CCR compression store statistics."""
        store = _server.get_compression_store()
        stats = store.get_stats()
        events = store.get_retrieval_events(limit=20)
        return {
            "store": stats,
            "recent_retrievals": [
                {
                    "hash": e.hash,
                    "query": e.query,
                    "items_retrieved": e.items_retrieved,
                    "total_items": e.total_items,
                    "tool_name": e.tool_name,
                    "retrieval_type": e.retrieval_type,
                }
                for e in events
            ],
        }

    @router.get("/v1/feedback", dependencies=[Depends(_require_loopback)])
    async def ccr_feedback() -> dict[str, Any]:
        """Get CCR feedback loop statistics and learned patterns.

        This endpoint exposes the feedback loop's learned patterns for monitoring
        and debugging. It shows:
        - Per-tool retrieval rates (high = compress less aggressively)
        - Common search queries per tool
        - Queried fields (suggest what to preserve)

        Use this to understand how well compression is working and whether
        the feedback loop is adjusting appropriately.
        """
        feedback = _server.get_compression_feedback()
        stats = feedback.get_stats()
        return {
            "feedback": stats,
            "hints_example": {
                tool_name: {
                    "hints": {
                        "max_items": hints.max_items
                        if (hints := feedback.get_compression_hints(tool_name))
                        else 15,
                        "suggested_items": hints.suggested_items if hints else None,
                        "skip_compression": hints.skip_compression if hints else False,
                        "preserve_fields": hints.preserve_fields if hints else [],
                        "reason": hints.reason if hints else "",
                    }
                }
                for tool_name in list(stats.get("tool_patterns", {}).keys())[:5]
            },
        }

    @router.get("/v1/feedback/{tool_name}", dependencies=[Depends(_require_loopback)])
    async def ccr_feedback_for_tool(tool_name: str) -> dict[str, Any]:
        """Get compression hints for a specific tool.

        Returns feedback-based hints that would be used for compressing
        this tool's output.
        """
        feedback = _server.get_compression_feedback()
        hints = feedback.get_compression_hints(tool_name)
        patterns = feedback.get_all_patterns().get(tool_name)

        return {
            "tool_name": tool_name,
            "hints": {
                "max_items": hints.max_items,
                "min_items": hints.min_items,
                "suggested_items": hints.suggested_items,
                "aggressiveness": hints.aggressiveness,
                "skip_compression": hints.skip_compression,
                "preserve_fields": hints.preserve_fields,
                "reason": hints.reason,
            },
            "pattern": {
                "total_compressions": patterns.total_compressions if patterns else 0,
                "total_retrievals": patterns.total_retrievals if patterns else 0,
                "retrieval_rate": patterns.retrieval_rate if patterns else 0.0,
                "full_retrieval_rate": patterns.full_retrieval_rate if patterns else 0.0,
                "search_rate": patterns.search_rate if patterns else 0.0,
                "common_queries": list(patterns.common_queries.keys())[:10] if patterns else [],
                "queried_fields": list(patterns.queried_fields.keys())[:10] if patterns else [],
            }
            if patterns
            else None,
        }

    @router.get("/config")
    async def get_config() -> JSONResponse:
        """Return dashboard-editable config (pricing overrides + settings)."""
        return JSONResponse(status_code=200, content=user_config.config_response())

    @router.post("/config", dependencies=[Depends(_require_loopback)])
    async def post_config(request: Request) -> JSONResponse:
        """Validate and persist dashboard config changes. Loopback-only.

        Body: ``{"pricing": {model: {input_per_1m, ...}}, "settings": {...}}``.
        Either section may be omitted; a provided section replaces the stored
        one. Unknown keys and negative numbers are rejected with 400.
        """
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(status_code=400, content={"error": "expected a JSON object"})
        try:
            user_config.update(body)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        logger.info("dashboard config updated via /config")
        return JSONResponse(
            status_code=200,
            content={"ok": True, **user_config.config_response()},
        )

    @router.get("/admin/deployments", dependencies=[Depends(_require_loopback)])
    async def admin_deployments() -> JSONResponse:
        """List LOCAL install profiles recorded on this machine.

        ponytail: these are local deployment manifests written by
        ``headroom install`` — NOT a multi-host fleet or remote deployments.
        """

        def _collect() -> list[dict[str, Any]]:
            from headroom.install.state import list_manifests

            out: list[dict[str, Any]] = []
            for manifest in list_manifests():
                out.append(
                    {
                        "profile": manifest.profile,
                        "updated_at": manifest.updated_at,
                        "artifact_count": len(getattr(manifest, "artifacts", []) or []),
                    }
                )
            return out

        return JSONResponse(content={"deployments": await asyncio.to_thread(_collect)})

    @router.get(
        "/mcp/dashboards/serena/summary",
        dependencies=[Depends(_require_loopback)],
    )
    async def mcp_serena_summary() -> JSONResponse:
        """Serena's live tool stats + active project, if it is reachable.

        Probes Serena's local dashboard API on 127.0.0.1:24282. On any error
        (not installed, not running, timeout) returns ``{"available": false}``.
        """

        def _fetch() -> dict[str, Any]:
            base = "http://127.0.0.1:24282"
            try:
                stats_resp = httpx.get(f"{base}/get_tool_stats", timeout=0.7)
                overview_resp = httpx.get(f"{base}/get_config_overview", timeout=0.7)
            except httpx.HTTPError:
                return {"available": False}
            if stats_resp.status_code >= 500 or overview_resp.status_code >= 500:
                return {"available": False}
            tool_stats: Any = None
            overview: Any = None
            try:
                tool_stats = stats_resp.json()
            except ValueError:
                tool_stats = None
            try:
                overview = overview_resp.json()
            except ValueError:
                overview = None
            active_project = None
            if isinstance(overview, dict):
                active_project = overview.get("active_project") or overview.get("project")
            return {
                "available": True,
                "active_project": active_project,
                "tool_stats": tool_stats,
            }

        return JSONResponse(content=await asyncio.to_thread(_fetch))

    @router.get("/stats/active_agents", dependencies=[Depends(_require_loopback)])
    async def stats_active_agents(window_seconds: int = 60) -> JSONResponse:
        """Agents seen in completed request logs within the recent window.

        ponytail: this is "recently active" derived from COMPLETED request
        logs — not live/open connections. An agent that finished a request
        just outside the window will not appear.
        """
        window = max(1, int(window_seconds))
        now = datetime.now()
        logs = proxy.logger.get_recent(500) if proxy and proxy.logger else []

        def _seconds_since(ts: str | None) -> float | None:
            if not ts:
                return None
            try:
                parsed = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                return None
            # Timestamps are written as naive datetime.now().isoformat();
            # drop any tzinfo so the subtraction stays naive-vs-naive.
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            return (now - parsed).total_seconds()

        # Filter to entries within the window, keyed by (agent, project).
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in logs:
            since = _seconds_since(entry.get("timestamp"))
            if since is None or since < 0 or since > window:
                continue
            agent_key, label, _source = _classify_agent_from_log(entry)
            raw_tags = entry.get("tags")
            tags = raw_tags if isinstance(raw_tags, dict) else {}
            project = str(tags.get("project") or "")
            gkey = (agent_key, project)
            before = max(0, int(entry.get("input_tokens_original") or 0))
            after = max(0, int(entry.get("input_tokens_optimized") or 0))
            saved = max(0, int(entry.get("tokens_saved") or 0))
            model = str(entry.get("model") or "unknown")

            row = groups.get(gkey)
            if row is None:
                row = {
                    "agent": agent_key,
                    "label": label,
                    "project": project,
                    "last_seen": entry.get("timestamp"),
                    "seconds_since_last": since,
                    "request_count": 0,
                    "tokens_saved": 0,
                    "before_tokens": 0,
                    "after_tokens": 0,
                    "models": {},
                }
                groups[gkey] = row
            row["request_count"] += 1
            row["tokens_saved"] += saved
            row["before_tokens"] += before
            row["after_tokens"] += after
            row["models"][model] = int(row["models"].get(model, 0)) + 1
            if since < row["seconds_since_last"]:
                row["seconds_since_last"] = since
                row["last_seen"] = entry.get("timestamp")

        agents = sorted(
            groups.values(),
            key=lambda r: r["seconds_since_last"],
        )
        return JSONResponse(content={"window_seconds": window, "agents": agents})

    @router.get("/learn/history", dependencies=[Depends(_require_loopback)])
    async def learn_history(limit: int = 50) -> JSONResponse:
        """Recent ``headroom learn`` runs recorded to the workspace."""
        from starlette.concurrency import run_in_threadpool

        from headroom.cli.learn_history import read_learn_history

        limit = max(1, min(limit, 200))
        runs = await run_in_threadpool(read_learn_history, limit)
        return JSONResponse(content={"runs": runs})

    @router.post("/learn/run", dependencies=[Depends(_require_loopback)])
    async def learn_run() -> JSONResponse:
        """Fire-and-forget ``headroom learn --apply`` from the Diagnostics pane.

        Spawned detached so this endpoint returns immediately -- a full learn
        pass can take a while, and there's nothing worth streaming back here.
        Progress is visible later via the existing /learn/history endpoint.
        """
        import subprocess
        import sys

        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "headroom.cli", "learn", "--apply"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return JSONResponse(content={"status": "started"})

    @router.get("/debug/memory/sync", dependencies=[Depends(_require_loopback)])
    async def debug_memory_sync() -> JSONResponse:
        """Per-agent memory sync counters from persisted sync state."""
        from headroom.memory.sync_stats import get_sync_stats

        return JSONResponse(content=get_sync_stats())

    return router
