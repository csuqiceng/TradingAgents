"""Technical indicators for the scalper strategy (pure functions, no deps).

All functions accept plain lists of floats and return ``float | None`` so
they can be unit-tested without a charting library and without any network.
NaN is never returned — ``None`` means "not enough data".
"""
from __future__ import annotations

from typing import Sequence


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    """Full EMA series (seed = SMA of the first period values).

    Returns a list the same length as ``values``; entries before enough data
    are ``None``.
    """
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def ema(values: Sequence[float], period: int) -> float | None:
    """Last value of the EMA series (or None when not enough data)."""
    series = ema_series(values, period)
    return series[-1] if series and series[-1] is not None else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI of the last ``period`` price changes."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if gains + losses == 0:
        return 50.0
    # Wilder's RSI: RS = avg_gain / avg_loss; period cancels out.
    if losses == 0:
        return 100.0
    if gains == 0:
        return 0.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD line, signal line and histogram (full series)."""
    fast_ema = ema_series(values, fast)
    slow_ema = ema_series(values, slow)
    macd_line: list[float | None] = []
    for f, s in zip(fast_ema, slow_ema):
        macd_line.append(f - s if f is not None and s is not None else None)
    valid = [m for m in macd_line if m is not None]
    if not valid:
        return macd_line, [None] * len(values), [None] * len(values)
    signal_line: list[float | None] = [None] * len(values)
    k = 2.0 / (signal + 1)
    # Seed the signal line with the first valid MACD value.
    first_idx = next(i for i, m in enumerate(macd_line) if m is not None)
    signal_line[first_idx] = macd_line[first_idx]
    prev = macd_line[first_idx]
    for i in range(first_idx + 1, len(values)):
        if macd_line[i] is not None:
            prev = macd_line[i] * k + prev * (1 - k)
            signal_line[i] = prev
    hist: list[float | None] = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, hist


def bollinger(
    values: Sequence[float],
    period: int = 20,
    n_std: float = 2.0,
) -> tuple[float | None, float | None, float | None]:
    """(lower, middle, upper) Bollinger bands."""
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance ** 0.5
    return mid - n_std * std, mid, mid + n_std * std


def _true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Wilder's ADX. Returns None when not enough data."""
    n = len(closes)
    if n < period * 2 + 1:
        return None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        trs.append(_true_range(highs[i], lows[i], closes[i - 1]))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    def wilder(series: list[float]) -> list[float]:
        out: list[float] = []
        acc = sum(series[:period])
        out.append(acc)
        for v in series[period:]:
            acc = acc - acc / period + v
            out.append(acc)
        return out

    atr = wilder(trs)
    pdi_series = wilder(plus_dm)
    mdi_series = wilder(minus_dm)
    dx_list: list[float] = []
    for atr_v, pdi, mdi in zip(atr, pdi_series, mdi_series):
        if atr_v <= 0:
            continue
        p = 100.0 * pdi / atr_v
        m = 100.0 * mdi / atr_v
        dx_list.append(100.0 * abs(p - m) / (p + m) if (p + m) > 0 else 0.0)
    if len(dx_list) < period:
        return None
    return sum(dx_list[-period:]) / period


def volume_ratio(volumes: Sequence[float], period: int = 20) -> float:
    """Current volume vs average of the previous ``period`` volumes."""
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-(period + 1):-1]) / period
    if avg <= 0:
        return 1.0
    return volumes[-1] / avg


def true_range_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> list[float]:
    """TR series for ATR-style volatility readings."""
    out: list[float] = []
    prev_close = None
    for h, l, c in zip(highs, lows, closes):
        out.append(_true_range(h, l, prev_close))
        prev_close = c
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Wilder's ATR of the last ``period`` true ranges."""
    trs = true_range_series(highs, lows, closes)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period
