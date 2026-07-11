"""Tests for headroom.proxy.user_config — the dashboard Settings menu's
persistent pricing/settings override store.

Covers the corruption/type-safety and info-leak fixes from the Copilot review
on the initial dashboard UX PR:
- a corrupted/hand-edited config file with non-dict `pricing`/`settings`
  must not crash callers, it should fall back to empty sections.
- bool values (JSON `true`/`false`) must never be treated as numeric pricing
  overrides, since `bool` is a subclass of `int` in Python.
- `config_response()` (served by the unauthenticated `GET /config` route)
  must never include the local filesystem config path.
"""

import json

import pytest

from headroom.proxy import user_config
from headroom.proxy.cost import CostTracker


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point the config store at a scratch dir and reset its in-memory cache."""
    monkeypatch.setenv("HEADROOM_CONFIG_DIR", str(tmp_path))
    user_config._cache = None
    yield
    user_config._cache = None


def _write_raw_config(tmp_path, payload: dict) -> None:
    (tmp_path / user_config.CONFIG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_load_falls_back_to_empty_when_pricing_is_not_a_dict(tmp_path):
    _write_raw_config(tmp_path, {"pricing": [], "settings": {}})
    data = user_config.load()
    assert data["pricing"] == {}


def test_load_falls_back_to_empty_when_pricing_is_a_non_empty_non_dict(tmp_path):
    # A truthy, non-dict value (e.g. a hand-edited list) must not pass through
    # unfiltered — `loaded.get("pricing") or {}` used to let this slip by.
    _write_raw_config(tmp_path, {"pricing": ["oops"], "settings": {}})
    data = user_config.load()
    assert data["pricing"] == {}


def test_load_falls_back_to_empty_when_settings_is_not_a_dict(tmp_path):
    _write_raw_config(tmp_path, {"pricing": {}, "settings": "not-a-dict"})
    data = user_config.load()
    assert data["settings"] == {}


def test_pricing_override_rejects_bool_input_per_1m(tmp_path):
    _write_raw_config(
        tmp_path,
        {"pricing": {"some-model": {"input_per_1m": True}}, "settings": {}},
    )
    assert user_config.pricing_override_per_token("some-model") is None


def test_pricing_override_falls_back_to_input_rate_when_output_is_bool(tmp_path):
    _write_raw_config(
        tmp_path,
        {
            "pricing": {"some-model": {"input_per_1m": 5.0, "output_per_1m": False}},
            "settings": {},
        },
    )
    result = user_config.pricing_override_per_token("some-model")
    assert result is not None
    # output_per_1m=False must be ignored (not treated as 0.0), falling back
    # to the input rate like an unset field would.
    assert result["output"] == result["input"]


def test_pricing_override_accepts_valid_numeric_entry(tmp_path):
    _write_raw_config(
        tmp_path,
        {
            "pricing": {
                "some-model": {
                    "input_per_1m": 3.0,
                    "output_per_1m": 15.0,
                    "cache_read_per_1m": 0.3,
                    "cache_write_per_1m": 3.75,
                }
            },
            "settings": {},
        },
    )
    result = user_config.pricing_override_per_token("some-model")
    assert result["input"] == pytest.approx(3.0 / 1_000_000)
    assert result["output"] == pytest.approx(15.0 / 1_000_000)
    assert result["cache_read"] == pytest.approx(0.3 / 1_000_000)
    assert result["cache_write"] == pytest.approx(3.75 / 1_000_000)


def test_cost_tracker_estimate_cost_uses_pricing_override(tmp_path):
    # Integration check: a Settings-menu pricing override must actually change
    # what CostTracker computes, not just what user_config returns in isolation.
    # Go through the real write API (same path POST /config uses) so the
    # in-memory cache is refreshed, matching how a running proxy behaves.
    model = "some-custom-model"
    tracker = CostTracker()
    baseline = tracker.estimate_cost(model, input_tokens=1_000_000, output_tokens=0)
    assert baseline is None  # unknown to LiteLLM and no override yet

    user_config.update({"pricing": {model: {"input_per_1m": 42.0}}})

    overridden = tracker.estimate_cost(model, input_tokens=1_000_000, output_tokens=0)
    assert overridden == pytest.approx(42.0)


def test_config_response_never_includes_local_filesystem_path(tmp_path):
    # GET /config is intentionally unauthenticated — it must never leak the
    # host's config file path to a non-loopback caller.
    payload = user_config.config_response()
    assert "config_path" not in payload
    assert set(payload.keys()) == {
        "pricing",
        "settings",
        "setting_defaults",
        "pricing_fields",
        "restart_required_settings",
    }


def test_budget_limit_zero_is_normalized_to_no_limit(tmp_path):
    # The Settings UI says "leave 0/blank for no limit" — submitting exactly
    # 0 must mean unlimited, not "budget is $0" (which would block all spend).
    user_config.update({"settings": {"budget_limit_usd": 0}})
    assert user_config.get_settings()["budget_limit_usd"] is None


def test_budget_limit_positive_value_is_kept(tmp_path):
    user_config.update({"settings": {"budget_limit_usd": 25}})
    assert user_config.get_settings()["budget_limit_usd"] == pytest.approx(25.0)


def test_cost_tracker_budget_remaining_is_none_only_when_limit_unset():
    from headroom.proxy.cost import CostTracker

    unlimited = CostTracker(budget_limit_usd=None)
    assert unlimited.stats()["budget_remaining_usd"] is None

    # A CostTracker constructed directly with budget_limit_usd=0.0 (bypassing
    # the Settings-menu normalization, e.g. via a CLI flag) must still report
    # a real remaining amount, not silently look "unlimited" via truthiness.
    zero_budget = CostTracker(budget_limit_usd=0.0)
    assert zero_budget.stats()["budget_remaining_usd"] == pytest.approx(0.0)


def test_pricing_override_rejects_zero_input_per_1m_at_write_time(tmp_path):
    # pricing_override_per_token() treats input_per_1m <= 0 as "no override",
    # so accepting 0 at write time would silently save a value that then
    # never takes effect. Must fail loudly instead.
    with pytest.raises(ValueError, match="input_per_1m must be > 0"):
        user_config.update({"pricing": {"some-model": {"input_per_1m": 0}}})


def test_pricing_override_allows_zero_for_other_fields(tmp_path):
    # Free cache reads etc. are legitimate; only input_per_1m has the > 0 rule.
    user_config.update({"pricing": {"some-model": {"input_per_1m": 3.0, "cache_read_per_1m": 0}}})
    result = user_config.pricing_override_per_token("some-model")
    assert result["cache_read"] == 0.0


def test_budget_remaining_usd_clamps_at_zero_when_over_budget(tmp_path):
    # check_budget() clamps remaining spend to >= 0; stats()'s
    # budget_remaining_usd must match, not go negative.
    model = "some-custom-model"
    user_config.update({"pricing": {model: {"input_per_1m": 100.0}}})
    tracker = CostTracker(budget_limit_usd=1.0)
    # 100.0 $/1M tokens * 1M tokens = $100 spent, way over the $1 budget.
    tracker.record_tokens(model, tokens_saved=0, tokens_sent=1_000_000, uncached_tokens=1_000_000)
    assert tracker.stats()["budget_remaining_usd"] == 0.0
