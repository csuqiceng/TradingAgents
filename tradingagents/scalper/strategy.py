"""Scalping strategy: 8-category signal scoring + 4-gate entry confirmation.

Re-implements the legacy scalp-trader logic:

- Each 15m candle is scored for long (``buy_score``) and short
  (``sell_score``) across 8 signal categories (RSI, volume, MACD, EMA trend,
  candle patterns, Bollinger touches, consecutive candles, price position).
- Entry requires all four gates for the favoured side:
    1. score >= threshold (default 2.5)
    2. ADX >= adx_min (short side relaxed by adx_min_short_offset, floor 15)
    3. volume ratio >= vol_min (default 1.5)
    4. EMA9 vs EMA21 alignment (trend_min_spread_pct)
- No data is fetched here: the caller passes OHLCV lists.

The strategy is a pure state machine; position management (stop-loss /
take-profit / trailing) lives in the loop using regime params.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import bollinger, ema, macd, rsi, volume_ratio, adx

# Candle named-tuple factory (kept compatible with plain dicts too).
def candle(close, high, low, open_, volume, timestamp=None):
    return {
        "close": float(close),
        "high": float(high),
        "low": float(low),
        "open": float(open_),
        "volume": float(volume),
        "timestamp": int(timestamp) if timestamp else 0,
    }


@dataclass
class StrategyConfig:
    """Tunable knobs for the scoring/entry logic."""
    buy_threshold: float = 2.5
    adx_min: float = 25.0
    adx_min_short_offset: float = 5.0   # short-side ADX = max(15, adx_min - offset)
    vol_min: float = 1.5
    trend_min_spread_pct: float = 0.05  # EMA9 vs EMA21 spread required
    min_candles: int = 30


@dataclass
class ScoreResult:
    buy_score: float = 0.0
    sell_score: float = 0.0
    adx_val: float | None = None
    vol_ratio: float = 1.0
    signals: list[str] = field(default_factory=list)


class ScalpStrategy:
    """15m short-term strategy: trend + signal + volume triple confirmation."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.name = "15m杠杆短线策略v2"
        self.config = config or StrategyConfig()

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #

    def evaluate(self, candles: list[dict]) -> ScoreResult:
        """Score the latest candle. Returns buy/sell scores + signals.

        ``candles`` is a list of dicts (see :func:`candle`) ordered oldest →
        newest, with at least ``min_candles`` entries.
        """
        cfg = self.config
        if len(candles) < cfg.min_candles:
            return ScoreResult()

        prices = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]
        price, prev_price = prices[-1], prices[-2]

        result = ScoreResult()
        result.adx_val = adx(highs, lows, prices, 14)
        result.vol_ratio = volume_ratio(volumes, 20)
        buy, sell = 0.0, 0.0
        signals: list[str] = []

        # 1. RSI ---------------------------------------------------------- #
        rsi14 = rsi(prices, 14)
        rsi6 = rsi(prices, 6)
        if rsi14 is not None:
            if rsi14 < 30:
                buy += 1.5
                signals.append(f"RSI超卖({rsi14:.0f})")
            elif rsi14 < 40:
                buy += 0.5
                signals.append(f"RSI偏低({rsi14:.0f})")
            elif rsi14 > 70:
                sell += 1.5
                signals.append(f"RSI超买({rsi14:.0f})")
            elif rsi14 > 60:
                sell += 0.5
                signals.append(f"RSI偏高({rsi14:.0f})")
            if rsi6 is not None:
                if rsi6 > rsi14 and rsi6 < 45:
                    buy += 0.5
                    signals.append("RSI6低位上穿")
                elif rsi6 < rsi14 and rsi6 > 55:
                    sell += 0.5
                    signals.append("RSI6高位下穿")

        # 2. Volume (directional) ----------------------------------------- #
        vol_r = result.vol_ratio
        if vol_r > 3.0:
            signals.append(f"巨量{vol_r:.1f}x")
            buy += 1.0 if price > prev_price else 0.0
            sell += 1.0 if price < prev_price else 0.0
        elif vol_r > 2.0:
            signals.append(f"放量{vol_r:.1f}x")
            buy += 0.5 if price > prev_price else 0.0
            sell += 0.5 if price < prev_price else 0.0
        elif vol_r < 0.5:
            signals.append(f"缩量{vol_r:.1f}x")

        # 3. MACD --------------------------------------------------------- #
        _, _, hist = macd(prices)
        if hist and len(hist) >= 3 and all(h is not None for h in hist[-3:]):
            h1, h2, h3 = hist[-3], hist[-2], hist[-1]
            if h2 < 0 and h3 > 0:
                buy += 1.0
                signals.append("MACD金叉")
            elif h2 > 0 and h3 < 0:
                sell += 1.0
                signals.append("MACD死叉")
            elif h3 > 0 and h3 > h2 > h1:
                buy += 0.5
                signals.append("MACD红柱放大")
            elif h3 < 0 and h3 < h2 < h1:
                sell += 0.5
                signals.append("MACD绿柱放大")
            elif h2 > 0 and h1 > 0 and h3 > h2:
                buy += 0.5
                signals.append("零轴上红柱")

        # 4. EMA trend ---------------------------------------------------- #
        ema9 = ema(prices, 9)
        ema21 = ema(prices, 21)
        if ema9 is not None and ema21 is not None:
            if price > ema9 > ema21:
                buy += 1.0
                signals.append("多头排列")
            elif price < ema9 < ema21:
                sell += 1.0
                signals.append("空头排列")
            elif len(prices) >= 3:
                prev9 = ema(prices[:-1], 9)
                prev21 = ema(prices[:-1], 21)
                if prev9 is not None and prev21 is not None:
                    if prev9 <= prev21 and ema9 > ema21:
                        buy += 0.5
                        signals.append("EMA金叉")
                    elif prev9 >= prev21 and ema9 < ema21:
                        sell += 0.5
                        signals.append("EMA死叉")

        # 5. Candle pattern ----------------------------------------------- #
        last = candles[-1]
        body = abs(last["close"] - last["open"])
        total = last["high"] - last["low"]
        if total > 0:
            upper_wick = last["high"] - max(last["close"], last["open"])
            lower_wick = min(last["close"], last["open"]) - last["low"]
            if lower_wick > body * 2 and upper_wick < body * 0.5:
                buy += 1.0
                signals.append("锤子线")
            elif upper_wick > body * 2 and lower_wick < body * 0.5:
                sell += 1.0
                signals.append("倒锤子线")
            elif body / total > 0.7 and last["close"] > last["open"]:
                buy += 0.5
                signals.append("大阳线")
            elif body / total > 0.7 and last["close"] < last["open"]:
                sell += 0.5
                signals.append("大阴线")

        # 6. Bollinger ---------------------------------------------------- #
        bl, bm, bu = bollinger(prices, 20, 2.0)
        if bl is not None and bu is not None:
            if price <= bl:
                buy += 0.5
                signals.append("触布林下轨")
            elif price >= bu:
                sell += 0.5
                signals.append("触布林上轨")

        # 7. Consecutive candles ------------------------------------------ #
        if len(candles) >= 5:
            consec_up = consec_down = 0
            for c in candles[-5:]:
                if c["close"] > c["open"]:
                    consec_up += 1
                    consec_down = 0
                elif c["close"] < c["open"]:
                    consec_down += 1
                    consec_up = 0
                else:
                    consec_up = consec_down = 0
            if consec_up >= 3:
                buy += 1.0
                signals.append(f"连涨{consec_up}根")
            elif consec_down >= 3:
                sell += 1.0
                signals.append(f"连跌{consec_down}根")
            elif consec_up >= 2:
                buy += 0.5
                signals.append(f"连涨{consec_up}根")
            elif consec_down >= 2:
                sell += 0.5
                signals.append(f"连跌{consec_down}根")

        # 8. Price position in recent range -------------------------------- #
        if len(candles) >= 10:
            recent_high = max(c["high"] for c in candles[-10:])
            recent_low = min(c["low"] for c in candles[-10:])
            price_range = recent_high - recent_low
            if price_range > 0:
                pos_pct = (price - recent_low) / price_range * 100
                if pos_pct < 20:
                    buy += 0.5
                    signals.append(f"低位区({pos_pct:.0f}%)")
                elif pos_pct > 80:
                    sell += 0.5
                    signals.append(f"高位区({pos_pct:.0f}%)")

        result.buy_score = round(max(0.0, buy), 1)
        result.sell_score = round(max(0.0, sell), 1)
        result.signals = signals
        return result

    # ------------------------------------------------------------------ #
    # Entry decision (4 gates)
    # ------------------------------------------------------------------ #

    def should_open(self, result: ScoreResult, closes: list[float]) -> str | None:
        """Return ``"long"`` / ``"short"`` when the entry gates pass, else None.

        ``closes`` is needed for the EMA alignment check. Gate order:
        score → ADX → volume → EMA direction.
        """
        cfg = self.config
        if result.buy_score < cfg.buy_threshold and result.sell_score < cfg.buy_threshold:
            return None

        # ADX gate (short side relaxed).
        if result.adx_val is not None:
            short_favored = result.sell_score > result.buy_score
            effective_adx_min = (
                max(15.0, cfg.adx_min - cfg.adx_min_short_offset)
                if short_favored
                else cfg.adx_min
            )
            if result.adx_val < effective_adx_min:
                return None

        # Volume gate.
        if result.vol_ratio < cfg.vol_min:
            return None

        # EMA direction gate.
        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        if ema9 is None or ema21 is None:
            return None
        spread = (ema9 - ema21) / ema21 * 100.0 if ema21 else 0.0

        if result.buy_score >= cfg.buy_threshold and result.buy_score > result.sell_score:
            if spread < cfg.trend_min_spread_pct:
                return None
            return "long"
        if result.sell_score >= cfg.buy_threshold and result.sell_score > result.buy_score:
            if spread > -cfg.trend_min_spread_pct:
                return None
            return "short"
        return None
