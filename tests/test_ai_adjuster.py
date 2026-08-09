"""Unit tests for the scalper DeepSeek AI adjuster (clamping / parsing)."""

from __future__ import annotations

import json

from tradingagents.scalper.ai_adjuster import (
    DEFAULT_PARAMS,
    compute_stats,
    current_effective_params,
    parse_adjustments,
    validate_and_clamp,
)


class TestComputeStats:
    def test_empty(self):
        stats = compute_stats([])
        assert stats["n"] == 0
        assert stats["win_rate"] == 0.0

    def test_basic(self):
        closes = [
            {"pnl_usdt": 1.0, "reason": "移动止盈", "regime": "bull", "symbol": "BTC-USDT-SWAP"},
            {"pnl_usdt": -0.5, "reason": "止损", "regime": "bull", "symbol": "ETH-USDT-SWAP"},
            {"pnl_usdt": 2.0, "reason": "止盈", "regime": "ranging", "symbol": "SOL-USDT-SWAP"},
        ]
        stats = compute_stats(closes)
        assert stats["n"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(66.7, abs=0.1)
        assert stats["total_pnl"] == pytest.approx(2.5)
        assert stats["by_reason"]["移动止盈"]["n"] == 1
        assert stats["by_regime"]["bull"]["pnl"] == pytest.approx(0.5)
        assert stats["by_symbol"]["SOL-USDT-SWAP"]["pnl"] == pytest.approx(2.0)


class TestParseAdjustments:
    def test_object_with_adjustments(self):
        text = '{"adjustments": [{"param": "strategy.buy_threshold", "value": 2.0}]}'
        out = parse_adjustments(text)
        assert out[0]["param"] == "strategy.buy_threshold"
        assert out[0]["value"] == 2.0

    def test_bare_array(self):
        text = '[{"param": "risk.position_pct", "value": 0.12}]'
        out = parse_adjustments(text)
        assert out[0]["value"] == 0.12

    def test_garbage(self):
        assert parse_adjustments("not json at all") == []
        assert parse_adjustments(None) == []

    def test_array_in_text(self):
        text = '分析完毕。结果如下：[{"param": "strategy.vol_min", "value": 1.8}] 结束'
        out = parse_adjustments(text)
        assert out[0]["param"] == "strategy.vol_min"


class TestValidateAndClamp:
    def test_unknown_path_dropped(self):
        approved, warnings = validate_and_clamp(
            [{"param": "nope", "value": 1.0}], {}
        )
        assert approved == []
        assert len(warnings) == 1

    def test_within_range_passes(self):
        approved, warnings = validate_and_clamp(
            [{"param": "strategy.buy_threshold", "value": 2.0}], {}
        )
        assert approved[0]["value"] == 2.0
        assert warnings == []

    def test_clamped_to_absolute_max(self):
        # buy_threshold default 2.5 → 150% = 3.75 (tightens before abs cap 4.0)
        approved, warnings = validate_and_clamp(
            [{"param": "strategy.buy_threshold", "value": 9.9}], {}
        )
        assert approved[0]["value"] == 3.75
        assert any("clamp" in w for w in warnings)

    def test_clamped_to_50pct_default(self):
        # adx_min default 25 → 50% = 12.5, but abs min 15 → 15
        approved, _ = validate_and_clamp(
            [{"param": "strategy.adx_min", "value": 5.0}], {}
        )
        assert approved[0]["value"] == 15.0
        # vol_min default 1.5 → 150% = 2.25, abs max 3 → 2.25
        approved2, _ = validate_and_clamp(
            [{"param": "strategy.vol_min", "value": 9.9}], {}
        )
        assert approved2[0]["value"] == 2.25

    def test_regime_params(self):
        approved, warnings = validate_and_clamp(
            [{"param": "regime.bull.BTC-USDT-SWAP.stop_loss_pct", "value": 8.0}], {}
        )
        assert approved[0]["value"] == 8.0
        assert warnings == []
        # take_profit hard floor 5
        approved2, _ = validate_and_clamp(
            [{"param": "regime.ranging.ETH-USDT-SWAP.take_profit_pct", "value": 2.0}], {}
        )
        assert approved2[0]["value"] == 5.0


class TestCurrentParams:
    def test_defaults_present(self):
        params = current_effective_params(_FakeStore())
        assert params["strategy.buy_threshold"] == DEFAULT_PARAMS["strategy.buy_threshold"]
        # regime params flattened
        assert "regime.bull.BTC-USDT-SWAP.stop_loss_pct" in params
        assert params["regime.bull.BTC-USDT-SWAP.stop_loss_pct"] == 6.0

    def test_merges_ai_config(self):
        store = _FakeStore(ai_config={"version": 123.0, "params": {"risk.position_pct": 0.12}})
        params = current_effective_params(store)
        assert params["risk.position_pct"] == 0.12


class _FakeStore:
    def __init__(self, ai_config: str | None = None) -> None:
        self._cfg = ai_config if ai_config is not None else ""

    def get_state(self, key: str, default: str = "") -> str:
        if key == "ai_config":
            return self._cfg if isinstance(self._cfg, str) else json.dumps(self._cfg)
        return default


import pytest  # noqa: E402  (imported after helper classes for readability)
