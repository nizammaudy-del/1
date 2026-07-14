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
    assert "scopeCompressionUnavailable" in html
    assert "scopeCacheUnavailable" in html
    assert "Unavailable for selected scope" in html
    assert "scope-proxy-saved-value" in html


def test_scope_selector_keeps_process_local_widgets_process_scoped() -> None:
    html = get_dashboard_html()

    assert "Current process only" in html
    assert "processLocalProviderCount" in html
    assert "Current process" in html
