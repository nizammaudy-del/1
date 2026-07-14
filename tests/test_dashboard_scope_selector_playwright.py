from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from headroom.dashboard import get_dashboard_html
from tests.test_dashboard_cache_ttl_playwright import _sample_history, _sample_stats

playwright = pytest.importorskip("playwright.sync_api")
Page = playwright.Page
expect = playwright.expect
sync_playwright = playwright.sync_playwright


def _stats_with_metric_scopes() -> dict:
    stats = copy.deepcopy(_sample_stats())
    stats["persistent_savings"]["display_session"] = {
        "tokens_saved": 222,
        "compression_savings_usd": 2.22,
        "cache_read_tokens": 2200,
        "cache_savings_usd": 0.88,
    }
    stats["persistent_savings"]["lifetime"] = {
        "tokens_saved": 333,
        "compression_savings_usd": 3.33,
        "cache_read_tokens": 3300,
        "cache_savings_usd": 1.44,
    }
    stats["metric_scopes"] = {
        "current_process": {
            "requests": {"total": 3},
            "compression": {
                "tokens_saved": 111,
                "compression_savings_usd": 1.11,
                "total_input_tokens": 900,
                "total_input_cost_usd": 0.33,
                "compressions_by_strategy": {"smart_crusher": 2},
                "tokens_saved_by_strategy": {"smart_crusher": 111},
            },
            "cache": {
                "cache_read_tokens": 1100,
                "cache_savings_usd": 0.55,
                "cache_write_tokens": 220,
                "cache_write_5m_tokens": 70,
                "cache_write_1h_tokens": 150,
                "cache_write_5m_requests": 2,
                "cache_write_1h_requests": 3,
                "uncached_input_tokens": 330,
                "requests": 3,
                "hit_requests": 2,
                "bust_count": 1,
                "bust_write_tokens": 80,
                "cache_bust_count": 1,
                "cache_bust_tokens_lost": 20,
                "hit_rate": 66.0,
                "request_hit_rate": 66.0,
                "observed_ttl_buckets": {
                    "5m": {"tokens": 70, "requests": 2},
                    "1h": {"tokens": 150, "requests": 3},
                },
                "observed_ttl_mix": {
                    "5m_pct": 31.8,
                    "1h_pct": 68.2,
                    "active_buckets": ["5m", "1h"],
                },
            },
            "codex_ws": {"units_total": 1, "frames_attempted_total": 1},
        },
        "display_session": {
            "requests": {"total": 6},
            "compression": {
                "tokens_saved": 222,
                "compression_savings_usd": 2.22,
                "total_input_tokens": 1800,
                "total_input_cost_usd": 0.66,
                "compressions_by_strategy": {"smart_crusher": 4},
                "tokens_saved_by_strategy": {"smart_crusher": 222},
            },
            "cache": {
                "cache_read_tokens": 2200,
                "cache_savings_usd": 0.88,
                "cache_write_tokens": 440,
                "cache_write_5m_tokens": 140,
                "cache_write_1h_tokens": 300,
                "cache_write_5m_requests": 4,
                "cache_write_1h_requests": 5,
                "uncached_input_tokens": 660,
                "requests": 6,
                "hit_requests": 4,
                "bust_count": 2,
                "bust_write_tokens": 160,
                "cache_bust_count": 2,
                "cache_bust_tokens_lost": 40,
                "hit_rate": 67.0,
                "request_hit_rate": 66.0,
                "observed_ttl_buckets": {
                    "5m": {"tokens": 140, "requests": 4},
                    "1h": {"tokens": 300, "requests": 5},
                },
                "observed_ttl_mix": {
                    "5m_pct": 31.8,
                    "1h_pct": 68.2,
                    "active_buckets": ["5m", "1h"],
                },
            },
            "codex_ws": {"units_total": 2, "frames_attempted_total": 2},
        },
        "lifetime": {
            "requests": {"total": 9},
            "compression": {
                "tokens_saved": 333,
                "compression_savings_usd": 3.33,
                "total_input_tokens": 2700,
                "total_input_cost_usd": 0.99,
                "compressions_by_strategy": {"smart_crusher": 6},
                "tokens_saved_by_strategy": {"smart_crusher": 333},
            },
            "cache": {
                "cache_read_tokens": 3300,
                "cache_savings_usd": 1.44,
                "cache_write_tokens": 660,
                "cache_write_5m_tokens": 210,
                "cache_write_1h_tokens": 450,
                "cache_write_5m_requests": 6,
                "cache_write_1h_requests": 7,
                "uncached_input_tokens": 990,
                "requests": 9,
                "hit_requests": 6,
                "bust_count": 3,
                "bust_write_tokens": 240,
                "cache_bust_count": 3,
                "cache_bust_tokens_lost": 60,
                "hit_rate": 68.0,
                "request_hit_rate": 66.0,
                "observed_ttl_buckets": {
                    "5m": {"tokens": 210, "requests": 6},
                    "1h": {"tokens": 450, "requests": 7},
                },
                "observed_ttl_mix": {
                    "5m_pct": 31.8,
                    "1h_pct": 68.2,
                    "active_buckets": ["5m", "1h"],
                },
            },
            "codex_ws": {"units_total": 3, "frames_attempted_total": 3},
        },
        "process_started_at": "2026-07-14T12:00:00Z",
        "updated_at": "2026-07-14T12:05:00Z",
    }
    return stats


def _stats_with_lifetime_only_cache_scope() -> dict:
    stats = _stats_with_metric_scopes()
    stats["metric_scopes"]["current_process"]["cache"] = {
        "cache_read_tokens": 0,
        "cache_savings_usd": 0.0,
        "cache_write_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cache_write_5m_requests": 0,
        "cache_write_1h_requests": 0,
        "uncached_input_tokens": 0,
        "requests": 0,
        "hit_requests": 0,
        "bust_count": 0,
        "bust_write_tokens": 0,
        "cache_bust_count": 0,
        "cache_bust_tokens_lost": 0,
        "hit_rate": 0.0,
        "request_hit_rate": 0.0,
        "observed_ttl_buckets": {
            "5m": {"tokens": 0, "requests": 0},
            "1h": {"tokens": 0, "requests": 0},
        },
        "observed_ttl_mix": {"5m_pct": 0.0, "1h_pct": 0.0, "active_buckets": []},
    }
    stats["prefix_cache"]["by_provider"] = {}
    stats["prefix_cache"]["totals"]["requests"] = 0
    stats["prefix_cache"]["totals"]["cache_read_tokens"] = 0
    stats["prefix_cache"]["totals"]["cache_write_tokens"] = 0
    stats["prefix_cache"]["totals"]["bust_count"] = 0
    return stats


def _stats_without_metric_scopes_lifetime_cache_only() -> dict:
    stats = copy.deepcopy(_sample_stats())
    stats["persistent_savings"]["lifetime"]["cache_read_tokens"] = 3300
    stats["persistent_savings"]["lifetime"]["cache_savings_usd"] = 1.44
    stats["prefix_cache"]["by_provider"] = {}
    stats["prefix_cache"]["totals"].update(
        {
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "cache_write_5m_requests": 0,
            "cache_write_1h_requests": 0,
            "requests": 0,
            "hit_requests": 0,
            "bust_count": 0,
            "bust_write_tokens": 0,
            "savings_usd": 0.0,
            "write_premium_usd": 0.0,
            "net_savings_usd": 0.0,
            "hit_rate": 0.0,
            "observed_ttl_buckets": {
                "5m": {"tokens": 0, "requests": 0},
                "1h": {"tokens": 0, "requests": 0},
            },
            "observed_ttl_mix": {"5m_pct": 0.0, "1h_pct": 0.0, "active_buckets": []},
        }
    )
    return stats


def _install_dashboard_routes(page: Page, stats: dict) -> None:
    history = _sample_history()
    health = {"status": "healthy", "version": "0.3.0"}
    dashboard_html = get_dashboard_html()

    def handler(route) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(route.request.url).path
        if path in ("/dashboard", "/"):
            route.fulfill(status=200, content_type="text/html", body=dashboard_html)
            return
        if "/stats-history" in path:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(history))
            return
        if path.endswith("/stats"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(stats))
            return
        if path.endswith("/health"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(health))
            return
        route.continue_()

    page.route("**/*", handler)


def _open_dashboard(page: Page, stats: dict) -> None:
    _install_dashboard_routes(page, stats)
    page.goto("http://headroom.local/dashboard", wait_until="load")
    page.wait_for_load_state("networkidle")


def _scope_cache_metric_value(page: Page, label: str):
    return page.locator(f"//div[normalize-space()='{label}']/following-sibling::div[1]")


def test_scope_selector_switches_aggregate_values() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_with_metric_scopes())

        expect(page.get_by_test_id("scope-lifetime")).to_be_visible()
        expect(page.get_by_test_id("scope-proxy-saved-value")).to_have_text("$3.33")
        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("3.3k")

        page.get_by_test_id("scope-session").click()
        expect(page.get_by_test_id("scope-proxy-saved-value")).to_have_text("$2.22")
        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("2.2k")

        page.get_by_test_id("scope-current").click()
        expect(page.get_by_test_id("scope-proxy-saved-value")).to_have_text("$1.11")
        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("1.1k")

        browser.close()


def test_lifetime_selection_keeps_process_local_widgets_process_scoped() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_with_lifetime_only_cache_scope())

        page.get_by_test_id("scope-lifetime").click()
        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("3.3k")
        expect(page.get_by_test_id("per-model-process-label")).to_have_text("Current process only")
        expect(page.get_by_text("Current process", exact=True)).to_have_count(3)

        browser.close()


def test_scope_selector_preserves_historical_view() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_with_metric_scopes())

        page.get_by_test_id("scope-current").click()
        page.get_by_role("button", name="Historical").click()
        expect(page.get_by_test_id("scope-current")).to_be_hidden()
        expect(page.get_by_text("Historical Proxy Compression")).to_be_visible()

        page.get_by_role("button", name="Session").click()
        expect(page.get_by_test_id("scope-current")).to_be_visible()
        expect(page.get_by_test_id("scope-proxy-saved-value")).to_have_text("$1.11")

        browser.close()


def test_current_scope_hides_cache_card_when_only_lifetime_has_cache() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_with_lifetime_only_cache_scope())

        expect(page.get_by_text("Prefix Cache Impact", exact=True)).to_be_visible()
        page.get_by_test_id("scope-current").click()
        expect(page.get_by_text("Prefix Cache Impact", exact=True)).to_have_count(0)

        browser.close()


def test_missing_selected_scope_cache_fields_render_unavailable() -> None:
    stats = _stats_with_metric_scopes()
    stats["metric_scopes"]["current_process"]["cache"] = {
        "cache_read_tokens": 1100,
        "cache_write_tokens": 220,
        "hit_rate": 66.0,
        "bust_count": 1,
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, stats)

        page.get_by_test_id("scope-current").click()
        expect(page.get_by_text("Unavailable for selected scope", exact=True)).to_be_visible()
        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("Unavailable")
        expect(page.get_by_text("Cache Efficiency", exact=True)).to_have_count(0)
        expect(page.get_by_test_id("cvc-net-headline")).to_have_text("Selected scope unavailable")
        expect(page.get_by_test_id("cvc-saved-value")).to_have_text("Unavailable")
        expect(page.get_by_test_id("cvc-bust-value")).to_have_text("Unavailable")
        expect(page.get_by_test_id("cvc-net-value")).to_have_text("Unavailable")

        browser.close()


def test_lifetime_scope_keeps_process_local_cache_breakdowns_labeled() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_with_metric_scopes())

        page.get_by_test_id("scope-lifetime").click()
        expect(page.get_by_text("Current process only", exact=True)).to_have_count(3)

        browser.close()


def test_legacy_lifetime_cache_keeps_process_local_cache_metrics_unavailable() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1800})
        _open_dashboard(page, _stats_without_metric_scopes_lifetime_cache_only())

        expect(page.get_by_test_id("scope-cache-reads-value")).to_have_text("3.3k")
        expect(_scope_cache_metric_value(page, "Cache Writes")).to_have_text("—")
        expect(_scope_cache_metric_value(page, "Hit Rate")).to_have_text("—")
        expect(_scope_cache_metric_value(page, "Cache Busts")).to_have_text("—")
        expect(page.get_by_text("no activity since restart", exact=True)).to_be_visible()
        expect(page.get_by_text("Cache Efficiency", exact=True)).to_have_count(0)

        browser.close()


def test_scope_selector_responsive_layout() -> None:
    artifact_dir = os.environ.get("HEADROOM_PLAYWRIGHT_ARTIFACT_DIR")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1800})
        _open_dashboard(page, _stats_with_metric_scopes())

        for width in (1280, 768, 400):
            page.set_viewport_size({"width": width, "height": 1800})
            expect(page.get_by_test_id("scope-current")).to_be_visible()
            expect(page.get_by_test_id("scope-session")).to_be_visible()
            expect(page.get_by_test_id("scope-lifetime")).to_be_visible()
            expect(page.get_by_test_id("scope-proxy-saved-value")).to_be_visible()

            if artifact_dir:
                Path(artifact_dir).mkdir(parents=True, exist_ok=True)
                page.screenshot(
                    path=str(Path(artifact_dir) / f"dashboard-scope-selector-{width}.png"),
                    full_page=True,
                )

        browser.close()
