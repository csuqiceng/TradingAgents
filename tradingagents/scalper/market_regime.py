"""Market regime classification (bull / bear / ranging) + adaptive params.

Re-implements the legacy scalp-trader market-regime logic: every N minutes
the BTC K-line is classified and the stop-loss / take-profit / trailing
parameters for each symbol switch to the matching regime's table.

Classification rules
--------------------
- ADX >= 30 (strong trend): EMA direction decides bull vs bear.
- 20 <= ADX < 30 (weak trend): 20-bar momentum + EMA direction.
- ADX < 20: ranging.

Parameters are expressed as **price-move percentages** (the same units the
legacy strategy used): stop_loss_pct is the adverse price move that triggers
the stop, take_profit_pct the favourable move that closes for profit, and
trailing_* are activation/callback percentages.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .indicators import adx, bollinger, ema, ema_series

# --------------------------------------------------------------------------- #
# Per-regime symbol parameters (price-move %, matching legacy tuning)
# --------------------------------------------------------------------------- #

REGIME_PARAMS: dict[str, dict[str, dict[str, float]]] = {
    "bull": {  # Uptrend: let profits run.
        "BTC-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 20.0, "trailing_activation": 6.0, "trailing_callback": 2.0},
        "ETH-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 20.0, "trailing_activation": 6.0, "trailing_callback": 2.5},
        "SOL-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 15.0, "trailing_activation": 6.0, "trailing_callback": 2.5},
    },
    "bear": {  # Downtrend: lock profits fast.
        "BTC-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 15.0, "trailing_activation": 6.0, "trailing_callback": 1.5},
        "ETH-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 15.0, "trailing_activation": 6.0, "trailing_callback": 2.0},
        "SOL-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 12.0, "trailing_activation": 6.0, "trailing_callback": 2.0},
    },
    "ranging": {  # Choppy: quick in/out.
        "BTC-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 10.0, "trailing_activation": 6.0, "trailing_callback": 1.0},
        "ETH-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 10.0, "trailing_activation": 6.0, "trailing_callback": 1.5},
        "SOL-USDT-SWAP": {"stop_loss_pct": 6.0, "take_profit_pct": 8.0, "trailing_activation": 6.0, "trailing_callback": 1.5},
    },
}

# Fallback for any symbol not in the table (e.g. a new coin added later).
_DEFAULT_PARAMS = {
    "stop_loss_pct": 6.0,
    "take_profit_pct": 10.0,
    "trailing_activation": 6.0,
    "trailing_callback": 1.5,
}


@dataclass
class RegimeCache:
    """Cached regime with TTL (re-classify at most every ``cache_seconds``)."""
    regime: str = "ranging"
    timestamp: float = 0.0
    cache_seconds: float = 1800.0  # 30 minutes


def classify_market(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> str:
    """Classify OHLC series into bull / bear / ranging."""
    if len(closes) < 50:
        return "ranging"
    adx_val = adx(highs, lows, closes, 14)
    if adx_val is None:
        return "ranging"
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    if ema9 is None or ema21 is None:
        return "ranging"
    price = closes[-1]
    ema50 = ema(closes, 50)
    momentum_20 = (
        (closes[-1] - closes[-20]) / closes[-20] * 100.0 if len(closes) >= 20 else 0.0
    )
    bl, bm, bu = bollinger(closes, 20, 2.0)

    if adx_val >= 30:  # Strong trend -> EMA direction decides.
        if ema9 > ema21 and (ema50 is None or price > ema50):
            return "bull"
        if ema9 < ema21 and (ema50 is None or price < ema50):
            return "bear"
        return "ranging"
    if adx_val >= 20:  # Weak trend -> momentum + EMA.
        if momentum_20 > 3 and ema9 > ema21:
            return "bull"
        if momentum_20 < -3 and ema9 < ema21:
            return "bear"
        return "ranging"
    return "ranging"


class MarketRegime:
    """Thread-safe-ish regime tracker with TTL caching."""

    def __init__(self, cache_seconds: float = 1800.0) -> None:
        self._cache = RegimeCache(cache_seconds=cache_seconds)

    def get(self) -> str:
        return self._cache.regime

    def update(
        self,
        closes: list[float],
        highs: list[float],
        lows: list[float],
        force: bool = False,
    ) -> str:
        now = time.time()
        if not force and now - self._cache.timestamp < self._cache.cache_seconds:
            return self._cache.regime
        regime = classify_market(closes, highs, lows)
        if regime != self._cache.regime:
            self._cache.regime = regime
        self._cache.timestamp = now
        return self._cache.regime


def get_params(symbol: str, regime: str) -> dict[str, float]:
    """Return the active SL/TP/trailing params for a symbol under a regime."""
    params = REGIME_PARAMS.get(regime, {}).get(symbol)
    if params is None:
        return dict(_DEFAULT_PARAMS)
    return dict(params)
