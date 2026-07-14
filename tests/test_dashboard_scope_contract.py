from __future__ import annotations

from headroom.dashboard import get_dashboard_html


def test_scope_selector_hides_without_metric_scopes() -> None:
    html = get_dashboard_html()

    assert "scopeMode: 'lifetime'" in html
    assert 'x-show="hasMetricScopes"' in html
    assert "if (!this.hasMetricScopes) return null;" in html
    assert "stats.persistent_savings?.lifetime?.compression_savings_usd" in html


def test_missing_selected_scope_field_renders_unavailable() -> None:
    html = get_dashboard_html()

    assert "get scopeCacheUnavailable()" in html
    assert "get scopeCacheStatusText()" in html
    assert "scopeCompressionUnavailable" in html
    assert "scopeCacheUnavailable" in html
    assert "Unavailable for selected scope" in html
    assert "typeof cache.cache_savings_usd === 'number'" in html
    assert "typeof cache.hit_requests === 'number'" in html
    assert "typeof cache.requests === 'number'" in html
    assert "typeof cache.bust_write_tokens === 'number'" in html
    assert "scope-proxy-saved-value" in html


def test_scope_selector_keeps_legacy_cache_fallback_unavailable() -> None:
    html = get_dashboard_html()

    assert "get legacyProcessCacheUnavailable()" in html
    assert "legacyProcessCacheUnavailable ? '—'" in html
    assert "no activity since restart" in html


def test_scope_selector_hides_incomplete_scope_cache_efficiency_and_flags_cvc() -> None:
    html = get_dashboard_html()

    assert 'x-show="cacheSessionActive && !scopeCacheUnavailable"' in html
    assert "scopeCacheUnavailable ? 'Selected scope unavailable'" in html
    assert 'data-testid="cvc-saved-value"' in html


def test_scope_selector_keeps_process_local_widgets_process_scoped() -> None:
    html = get_dashboard_html()

    assert "Current process only" in html
    assert "processLocalProviderCount" in html
    assert "Current process" in html


def test_scope_selector_keeps_cache_card_selected_scope_aware() -> None:
    html = get_dashboard_html()

    assert "return this.scopedCacheActive;" in html
    assert "Net savings: $" in html
    assert "no activity since restart" in html
