from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom.proxy.prometheus_metrics import PrometheusMetrics
from headroom.proxy.savings_tracker import SavingsTracker
from headroom.proxy.server import ProxyConfig, create_app


def _record_proxy_request(
    proxy,
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 120,
    output_tokens: int = 24,
    tokens_saved: int = 30,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    uncached_input_tokens: int = 0,
) -> None:
    if proxy.cost_tracker:
        proxy.cost_tracker.record_tokens(model, tokens_saved, input_tokens)
    asyncio.run(
        proxy.metrics.record_request(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tokens_saved=tokens_saved,
            latency_ms=15.0,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
            uncached_input_tokens=uncached_input_tokens,
        )
    )


def test_metric_scopes_survive_restart_and_current_process_resets(tmp_path, monkeypatch):
    savings_path = tmp_path / "proxy_savings.json"
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(cache_enabled=False, rate_limit_enabled=False, log_requests=False)

    with TestClient(create_app(config)) as client:
        proxy = client.app.state.proxy
        _record_proxy_request(
            proxy,
            cache_read_tokens=40,
            cache_write_tokens=60,
            cache_write_5m_tokens=10,
            cache_write_1h_tokens=50,
            uncached_input_tokens=20,
        )
        _record_proxy_request(
            proxy,
            cache_read_tokens=10,
            cache_write_tokens=90,
            cache_write_5m_tokens=15,
            cache_write_1h_tokens=75,
            uncached_input_tokens=30,
        )
        proxy.metrics.record_compression("smart_crusher", 100, 70)
        proxy.metrics.record_compression("smart_crusher", 80, 50)
        proxy.metrics.record_codex_ws_unit(
            strategy="smart_crusher",
            reason_category="tool_result",
            elapsed_ms=5.0,
            text_bytes=128,
            tokens_before=100,
            tokens_after=70,
            tokens_saved=30,
            modified=True,
            strategy_chain=["smart_crusher"],
            content_type="text",
            text_shape="json",
        )
        proxy.metrics.record_codex_ws_frame(
            elapsed_ms=9.0,
            bytes_before=256,
            bytes_after=180,
            attempted_tokens=100,
            tokens_saved=30,
            modified=True,
            strategy_chain=["smart_crusher"],
            final_strategies=["smart_crusher"],
        )
        asyncio.run(proxy.metrics.record_cache_bust(11))

        live_stats = client.get("/stats").json()
        live_scope = live_stats["metric_scopes"]
        assert live_scope["current_process"]["requests"]["total"] == 2
        assert live_scope["current_process"]["cache"]["requests"] == 2
        assert live_scope["current_process"]["cache"]["bust_count"] == 1
        assert live_scope["current_process"]["cache"]["cache_bust_count"] == 1
        assert live_scope["current_process"]["compression"]["tokens_saved"] == 60
        assert live_scope["current_process"]["compression"]["compressions_by_strategy"] == {
            "smart_crusher": 2
        }
        assert live_scope["current_process"]["codex_ws"]["units_total"] == 1
        assert live_scope["display_session"]["requests"]["total"] == 2
        assert live_scope["lifetime"]["requests"]["total"] == 2
        assert live_scope["lifetime"]["cache"]["cache_write_tokens"] == 150
        assert live_scope["lifetime"]["cache"]["cache_write_1h_tokens"] == 125
        assert live_scope["lifetime"]["compression"]["compressions_by_strategy"] == {
            "smart_crusher": 2
        }
        assert live_scope["lifetime"]["codex_ws"]["frames_attempted_total"] == 1
        assert live_scope["updated_at"] is not None
        assert live_scope["process_started_at"] is not None

    with TestClient(create_app(config)) as client:
        restarted = client.get("/stats").json()
        scope = restarted["metric_scopes"]
        assert scope["current_process"]["requests"]["total"] == 0
        assert scope["current_process"]["compression"]["tokens_saved"] == 0
        assert scope["current_process"]["codex_ws"]["units_total"] == 0
        assert scope["display_session"]["requests"]["total"] == 2
        assert scope["lifetime"]["requests"]["total"] == 2
        assert scope["lifetime"]["cache"]["requests"] == 2
        assert scope["lifetime"]["cache"]["hit_requests"] == 2
        assert scope["lifetime"]["cache"]["cache_read_tokens"] == 50
        assert scope["lifetime"]["cache"]["cache_write_tokens"] == 150
        assert scope["lifetime"]["cache"]["cache_write_5m_tokens"] == 25
        assert scope["lifetime"]["cache"]["cache_write_1h_tokens"] == 125
        assert scope["lifetime"]["cache"]["uncached_input_tokens"] == 50
        assert scope["lifetime"]["cache"]["bust_count"] == 1
        assert scope["lifetime"]["cache"]["cache_bust_count"] == 1
        assert scope["lifetime"]["cache"]["cache_bust_tokens_lost"] == 11
        assert scope["lifetime"]["compression"]["compressions_by_strategy"] == {"smart_crusher": 2}
        assert scope["lifetime"]["compression"]["tokens_saved_by_strategy"] == {"smart_crusher": 60}
        assert scope["lifetime"]["codex_ws"]["units_total"] == 1
        assert scope["lifetime"]["codex_ws"]["frames_attempted_total"] == 1

        metrics = client.get("/metrics").text
        assert "headroom_requests_total 0" in metrics
        assert "headroom_persistent_savings_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_read_tokens_total 50" in metrics
        assert "headroom_persistent_savings_cache_write_tokens_total 150" in metrics
        assert "headroom_persistent_savings_cache_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_hit_requests_total 2" in metrics
        assert "headroom_persistent_savings_cache_bust_total 1" in metrics
        assert "headroom_persistent_savings_cache_bust_tokens_lost_total 11" in metrics


def test_metric_scopes_migrate_v4_state_additively(tmp_path):
    path = tmp_path / "proxy_savings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "lifetime": {
                    "requests": 3,
                    "tokens_saved": 90,
                    "compression_savings_usd": 0.27,
                    "cache_read_tokens": 50,
                    "cache_savings_usd": 0.12,
                    "total_input_tokens": 360,
                    "total_input_cost_usd": 0.81,
                },
                "display_session": {
                    "requests": 1,
                    "tokens_saved": 30,
                    "compression_savings_usd": 0.09,
                    "cache_read_tokens": 10,
                    "cache_savings_usd": 0.03,
                    "total_input_tokens": 120,
                    "total_input_cost_usd": 0.27,
                    "started_at": "2026-07-14T12:00:00Z",
                    "last_activity_at": "2026-07-14T12:00:00Z",
                },
                "history": [],
                "projects": {},
                "by_model": {},
            }
        ),
        encoding="utf-8",
    )

    tracker = SavingsTracker(path=str(path))
    snapshot = tracker.snapshot()

    assert snapshot["schema_version"] == 5
    assert snapshot["lifetime"]["requests"] == 3
    assert snapshot["lifetime"]["aggregates"]["cache"]["cache_write_tokens"] == 0
    assert snapshot["lifetime"]["aggregates"]["compression"]["compressions_by_strategy"] == {}
    assert snapshot["lifetime"]["aggregates"]["codex_ws"]["units_total"] == 0
    assert snapshot["display_session"]["aggregates"]["cache"]["requests"] == 0


def test_metric_scopes_stateless_mode_writes_nothing_and_restarts_empty(tmp_path, monkeypatch):
    savings_path = tmp_path / "proxy_savings.json"
    monkeypatch.setenv("HEADROOM_SAVINGS_PATH", str(savings_path))
    config = ProxyConfig(
        cache_enabled=False,
        rate_limit_enabled=False,
        log_requests=False,
        stateless=True,
    )

    with TestClient(create_app(config)) as client:
        proxy = client.app.state.proxy
        _record_proxy_request(proxy, tokens_saved=25, cache_read_tokens=15, cache_write_tokens=20)
        proxy.metrics.record_compression("smart_crusher", 100, 75)
        stats = client.get("/stats").json()
        assert stats["metric_scopes"]["current_process"]["requests"]["total"] == 1
        assert stats["metric_scopes"]["lifetime"]["requests"]["total"] == 1

    assert not savings_path.exists()

    with TestClient(create_app(config)) as client:
        stats = client.get("/stats").json()
        assert stats["metric_scopes"]["current_process"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["requests"]["total"] == 0
        assert stats["metric_scopes"]["lifetime"]["compression"]["compressions_by_strategy"] == {}


def test_aggregate_only_updates_stay_dirty_until_request_save(tmp_path):
    path = tmp_path / "proxy_savings.json"
    tracker = SavingsTracker(path=str(path), save_flush_every=2)

    tracker.record_compression(
        strategy="smart_crusher",
        original_tokens=100,
        compressed_tokens=70,
        timestamp="2026-07-14T12:00:00Z",
    )
    tracker.record_cache_bust(tokens_lost=11, timestamp="2026-07-14T12:00:01Z")
    tracker.record_codex_ws_unit(
        strategy="smart_crusher",
        reason_category="tool_result",
        elapsed_ms=5.0,
        text_bytes=128,
        tokens_before=100,
        tokens_after=70,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        content_type="text",
        text_shape="json",
        timestamp="2026-07-14T12:00:02Z",
    )
    tracker.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
        timestamp="2026-07-14T12:00:03Z",
    )

    assert not path.exists()

    tracker.record_request(
        model="gpt-4o",
        input_tokens=120,
        tokens_saved=30,
        timestamp="2026-07-14T12:00:04Z",
    )
    assert not path.exists()

    tracker.record_request(
        model="gpt-4o",
        input_tokens=80,
        tokens_saved=10,
        timestamp="2026-07-14T12:00:05Z",
    )

    persisted = json.loads(path.read_text())
    lifetime = persisted["lifetime"]
    cache = lifetime["aggregates"]["cache"]
    compression = lifetime["aggregates"]["compression"]
    codex_ws = lifetime["aggregates"]["codex_ws"]
    assert compression["compressions_by_strategy"] == {"smart_crusher": 1}
    assert compression["tokens_saved_by_strategy"] == {"smart_crusher": 30}
    assert cache["cache_bust_count"] == 1
    assert cache["cache_bust_tokens_lost"] == 11
    assert codex_ws["units_total"] == 1
    assert codex_ws["frames_attempted_total"] == 1


def test_graceful_flush_persists_aggregate_only_updates(tmp_path):
    path = tmp_path / "proxy_savings.json"
    metrics = PrometheusMetrics(savings_tracker=SavingsTracker(path=str(path), save_flush_every=25))

    metrics.record_compression("smart_crusher", 100, 70)
    asyncio.run(metrics.record_cache_bust(11))
    metrics.record_codex_ws_frame(
        elapsed_ms=9.0,
        bytes_before=256,
        bytes_after=180,
        attempted_tokens=100,
        tokens_saved=30,
        modified=True,
        strategy_chain=["smart_crusher"],
        final_strategies=["smart_crusher"],
    )
    assert not path.exists()

    metrics.savings_tracker.flush()
    persisted = json.loads(path.read_text())
    assert persisted["lifetime"]["aggregates"]["compression"]["compressions_by_strategy"] == {
        "smart_crusher": 1
    }
    assert persisted["lifetime"]["aggregates"]["cache"]["cache_bust_count"] == 1
    assert persisted["lifetime"]["aggregates"]["codex_ws"]["frames_attempted_total"] == 1
