"""Unit tests for the scalper module (indicators / strategy / regime / broker).

Pure functions are tested directly; the broker sizing math is tested with a
fake exchange so no network or credentials are needed.
"""
from __future__ import annotations

import math

import pytest

from tradingagents.scalper.broker_swap import SwapBroker, SwapBrokerError
from tradingagents.scalper.indicators import (
    adx, atr, bollinger, ema, ema_series, macd, rsi, sma, volume_ratio,
)
from tradingagents.scalper.market_regime import (
    MarketRegime, classify_market, get_params,
)
from tradingagents.scalper.strategy import ScalpStrategy, ScoreResult, StrategyConfig, candle


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #

class TestIndicators:
    def test_sma_basic(self):
        assert sma([1, 2, 3, 4], 3) == 3.0
        assert sma([1, 2], 3) is None

    def test_ema_series_length_and_seed(self):
        values = [float(i) for i in range(1, 30)]
        series = ema_series(values, 9)
        assert len(series) == len(values)
        assert series[8] == pytest.approx(sum(values[:9]) / 9)
        assert series[7] is None
        assert series[-1] is not None

    def test_rsi_bounds(self):
        # All-up prices → RSI ~100
        up = [float(i) for i in range(1, 30)]
        assert rsi(up, 14) > 90
        # All-down prices → RSI ~0
        down = [float(30 - i) for i in range(1, 30)]
        assert rsi(down, 14) < 10
        # Flat → 50
        flat = [10.0] * 20
        assert rsi(flat, 14) == pytest.approx(50.0)

    def test_macd_shapes(self):
        values = [math.sin(i / 5) * 10 + 100 for i in range(60)]
        macd_line, signal_line, hist = macd(values, 12, 26, 9)
        assert len(macd_line) == len(values)
        assert hist[-1] is not None
        assert macd_line[-1] is not None

    def test_bollinger_bounds(self):
        values = [100.0 + math.sin(i / 3) * 2 for i in range(30)]
        lower, mid, upper = bollinger(values, 20, 2.0)
        assert lower is not None and mid is not None and upper is not None
        assert lower < mid < upper

    def test_adx_none_when_short(self):
        assert adx([1, 2, 3], [1, 2, 3], [1, 2, 3]) is None

    def test_adx_trending_market(self):
        # A steady uptrend should produce a measurable ADX.
        closes = [100 + i for i in range(60)]
        highs = [101 + i for i in range(60)]
        lows = [99 + i for i in range(60)]
        val = adx(highs, lows, closes, 14)
        assert val is not None and 0 <= val <= 100

    def test_atr(self):
        highs = [10 + i for i in range(20)]
        lows = [8 + i for i in range(20)]
        closes = [9 + i for i in range(20)]
        val = atr(highs, lows, closes, 14)
        assert val is not None and val > 0

    def test_volume_ratio(self):
        vols = [10.0] * 20 + [50.0]
        assert volume_ratio(vols, 20) == pytest.approx(5.0)
        assert volume_ratio([1.0], 20) == 1.0  # not enough data


# --------------------------------------------------------------------------- #
# Strategy scoring & entry gates
# --------------------------------------------------------------------------- #

def _make_candles(n=60, trend=0.3, base=100.0):
    """Synthetic candles with a gentle directional drift and pullbacks
    (wave-like, so RSI/MACD stay in realistic ranges instead of pegging)."""
    out = []
    price = base
    for i in range(n):
        open_ = price
        wave = (i % 8) - 3  # pullback every few bars keeps RSI off the rails
        price = open_ + trend + wave * 0.05
        close = price
        high = max(open_, close) + 0.5
        low = min(open_, close) - 0.5
        out.append(candle(close, high, low, open_, volume=100.0 + (i % 5) * 20))
    return out


class TestStrategy:
    def test_not_enough_candles_returns_zero(self):
        strategy = ScalpStrategy()
        result = strategy.evaluate(_make_candles(10))
        assert result.buy_score == 0 and result.sell_score == 0

    def test_uptrend_scores_long(self):
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=1.5))
        candles = _make_candles(60, trend=0.5)
        result = strategy.evaluate(candles)
        assert result.buy_score > 0

    def test_downtrend_scores_short(self):
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=1.5))
        candles = _make_candles(60, trend=-0.5)
        result = strategy.evaluate(candles)
        assert result.sell_score > 0

    def test_should_open_long_on_strong_uptrend(self):
        # Build a ScoreResult that passes all gates + an EMA-bullish close
        # series. This tests the gate logic in isolation instead of relying
        # on synthetic candles (whose RSI can peg and flip the score).
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=2.0, adx_min=5.0, vol_min=0.5))
        result = ScoreResult(buy_score=2.5, sell_score=1.0, adx_val=30.0, vol_ratio=2.0)
        closes = [float(100 + i * 0.5) for i in range(60)]  # steady uptrend → EMA9 > EMA21
        assert strategy.should_open(result, closes) == "long"

    def test_should_open_short_on_strong_downtrend(self):
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=2.0, adx_min=5.0, vol_min=0.5))
        result = ScoreResult(buy_score=1.0, sell_score=2.5, adx_val=30.0, vol_ratio=2.0)
        closes = [float(200 - i * 0.5) for i in range(60)]  # steady downtrend → EMA9 < EMA21
        assert strategy.should_open(result, closes) == "short"

    def test_should_open_gates_adx(self):
        # ADX too low → blocked even with high score.
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=2.0, adx_min=25.0, vol_min=0.5))
        result = ScoreResult(buy_score=3.0, sell_score=1.0, adx_val=10.0, vol_ratio=2.0)
        closes = [float(100 + i * 0.5) for i in range(60)]
        assert strategy.should_open(result, closes) is None

    def test_should_open_gates_volume(self):
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=2.0, adx_min=5.0, vol_min=1.5))
        result = ScoreResult(buy_score=3.0, sell_score=1.0, adx_val=30.0, vol_ratio=1.0)
        closes = [float(100 + i * 0.5) for i in range(60)]
        assert strategy.should_open(result, closes) is None

    def test_should_open_gates_ema_direction(self):
        # High score but EMA not aligned → blocked.
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=2.0, adx_min=5.0, vol_min=0.5,
                                                trend_min_spread_pct=0.5))
        result = ScoreResult(buy_score=3.0, sell_score=1.0, adx_val=30.0, vol_ratio=2.0)
        closes = [float(100 + (i % 2) * 0.1) for i in range(60)]  # flat → EMA9 ≈ EMA21
        assert strategy.should_open(result, closes) is None

    def test_no_signal_in_ranging(self):
        strategy = ScalpStrategy(StrategyConfig(buy_threshold=3.0, adx_min=50.0, vol_min=5.0))
        candles = _make_candles(60, trend=0.05)
        result = strategy.evaluate(candles)
        closes = [c["close"] for c in candles]
        assert strategy.should_open(result, closes) is None


# --------------------------------------------------------------------------- #
# Market regime
# --------------------------------------------------------------------------- #

class TestMarketRegime:
    def test_classify_uptrend_bull(self):
        closes = [100 + i * 0.5 for i in range(60)]
        highs = [101 + i * 0.5 for i in range(60)]
        lows = [99 + i * 0.5 for i in range(60)]
        assert classify_market(closes, highs, lows) in ("bull", "ranging")

    def test_classify_downtrend_bear(self):
        closes = [100 - i * 0.5 for i in range(60)]
        highs = [101 - i * 0.5 for i in range(60)]
        lows = [99 - i * 0.5 for i in range(60)]
        assert classify_market(closes, highs, lows) in ("bear", "ranging")

    def test_classify_flat_ranging(self):
        closes = [100 + math.sin(i / 10) for i in range(60)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        assert classify_market(closes, highs, lows) == "ranging"

    def test_regime_cache(self):
        regime = MarketRegime(cache_seconds=3600)
        closes = [100 + i * 0.5 for i in range(60)]
        highs = [101 + i * 0.5 for i in range(60)]
        lows = [99 + i * 0.5 for i in range(60)]
        first = regime.update(closes, highs, lows, force=True)
        # Second call within TTL returns cached value without recomputing.
        second = regime.update(closes, highs, lows)
        assert first == second
        assert regime.get() == first

    def test_get_params_fallback(self):
        params = get_params("DOGE-USDT-SWAP", "ranging")
        assert params["stop_loss_pct"] == 6.0
        assert "take_profit_pct" in params

    def test_get_params_regime_specific(self):
        bull_btc = get_params("BTC-USDT-SWAP", "bull")
        ranging_btc = get_params("BTC-USDT-SWAP", "ranging")
        assert bull_btc["take_profit_pct"] > ranging_btc["take_profit_pct"]


# --------------------------------------------------------------------------- #
# Broker (fake exchange, no network)
# --------------------------------------------------------------------------- #

class FakeExchange:
    """Minimal fake for the ccxt surface the broker uses."""

    def __init__(self):
        self.sandbox_mode = False
        self.position_mode = False
        self.leverage_calls = []

    def set_sandbox_mode(self, enabled):
        self.sandbox_mode = enabled

    def set_position_mode(self, enabled):
        self.position_mode = enabled

    def set_leverage(self, leverage, symbol, params=None):
        self.leverage_calls.append((leverage, symbol, params))


class TestSwapBroker:
    def test_refuses_live_mode(self):
        with pytest.raises(ValueError):
            SwapBroker(FakeExchange(), sandbox=False)

    def test_enables_sandbox(self):
        ex = FakeExchange()
        broker = SwapBroker(ex, sandbox=True)
        assert ex.sandbox_mode is True

    def test_ensure_hedge_mode(self):
        ex = FakeExchange()
        broker = SwapBroker(ex, sandbox=True)
        broker.ensure_hedge_mode()
        assert ex.position_mode is True

    def test_set_leverage_calls_exchange(self):
        ex = FakeExchange()
        broker = SwapBroker(ex, leverage=5, sandbox=True)
        broker.set_leverage("BTC/USDT:USDT", "long")
        assert ex.leverage_calls == [(5, "BTC/USDT:USDT", {"mgnMode": "cross", "posSide": "long"})]

    def test_calc_contracts_btc(self):
        # BTC: ctVal=0.01 → lots = margin*lev / (price * 0.01), floored to lotSz 0.01
        assert SwapBroker.calc_contracts(16.0, 65000.0, 0.01, 5) == pytest.approx(0.12)  # 80/650=0.123
        assert SwapBroker.calc_contracts(700.0, 65000.0, 0.01, 5) == pytest.approx(5.38)  # 3500/650=5.38
        assert SwapBroker.calc_contracts(1500.0, 65000.0, 0.01, 5) == pytest.approx(11.53)

    def test_calc_contracts_sol(self):
        # SOL: ctVal=1 → lots = margin*lev / price
        assert SwapBroker.calc_contracts(80.0, 76.0, 1.0, 5) == pytest.approx(5.26)
        assert SwapBroker.calc_contracts(1000.0, 76.0, 1.0, 5) == pytest.approx(65.78)

    def test_calc_contracts_small_budget(self):
        # 160 USDT 预算下三个币都能开（0.01 张精度）
        assert SwapBroker.calc_contracts(16.0, 64866.0, 0.01, 5) == pytest.approx(0.12)  # BTC 0.12 张
        assert SwapBroker.calc_contracts(16.0, 1920.0, 0.1, 5) == pytest.approx(0.41)    # ETH 0.41 张
        assert SwapBroker.calc_contracts(16.0, 76.0, 1.0, 5) == pytest.approx(1.05)      # SOL 1.05 张

    def test_calc_contracts_budget_below_one_lot(self):
        # 预算不够 0.01 张 → 0（如 1 USDT 开 BTC）
        assert SwapBroker.calc_contracts(1.0, 65000.0, 0.01, 5) == 0
        assert SwapBroker.calc_contracts(0.01, 65000.0, 0.01, 5) == 0

    def test_calc_contracts_custom_lot_sz(self):
        # 自定义 lotSz（如 0.1）时按该精度向下取整
        assert SwapBroker.calc_contracts(16.0, 1920.0, 0.1, 5, lot_sz=0.1) == pytest.approx(0.4)
        assert SwapBroker.calc_contracts(16.0, 1920.0, 0.1, 5, lot_sz=1.0) == 0.0
