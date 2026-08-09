"""DeepSeek-driven periodic parameter adjustment for the leverage scalper.

Standalone module (run via cron, independent of the 5s loop process):

1. Reads recent closed trades from the scalper SQLite store.
2. Computes win-rate / PnL / reason / regime / symbol statistics.
3. Sends the stats + current parameter table to DeepSeek, which returns a
   JSON list of parameter adjustments (or ``[]`` when nothing is justified).
4. Every value is validated and clamped to safe ranges (±50% of default,
   absolute min/max). The scalper's own hard limits (buy_threshold cap,
   position sizing floors) can never be violated.
5. Writes the approved adjustment set to the store under ``ai_config``; the
   running ``ScalperLoop`` hot-applies it within ~60s (no restart needed).

Safety rules inherited from the legacy AI adjuster:
- Perpetual (swap) strategy: AI can never pause trading or zero the entry
  threshold — ``buy_threshold`` is clamped to max 4.0.
- In panic (win-rate < 30% or loss streak >= 3) the AI is told to only
  shrink position size / raise entry threshold, never loosen stops.
- ``notional_capital`` and ``leverage`` are deliberately NOT adjustable.

Usage
-----
    python -m tradingagents.scalper.ai_adjuster [--dry-run] [--min-trades 3]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.request
from collections import defaultdict

from .market_regime import REGIME_PARAMS
from .scalper_loop import ScalperConfig, feishu_send
from .state_store import ScalperStore
from .strategy import StrategyConfig

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Adjustable parameter table: path -> (min, max, default)
# --------------------------------------------------------------------------- #

DEFAULT_PARAMS: dict[str, float] = {
    # Strategy entry gates (strategy.config)
    "strategy.buy_threshold": StrategyConfig().buy_threshold,
    "strategy.adx_min": StrategyConfig().adx_min,
    "strategy.adx_min_short_offset": StrategyConfig().adx_min_short_offset,
    "strategy.vol_min": StrategyConfig().vol_min,
    "strategy.trend_min_spread_pct": StrategyConfig().trend_min_spread_pct,
    # Risk / sizing (ScalperConfig)
    "risk.position_pct": ScalperConfig().position_pct,
    "risk.max_total_pct": ScalperConfig().max_total_pct,
    "risk.max_positions": ScalperConfig().max_positions,
    "risk.cooldown_seconds": ScalperConfig().cooldown_seconds,
    "risk.max_consecutive_losses": ScalperConfig().max_consecutive_losses,
    "risk.pause_minutes": ScalperConfig().pause_minutes,
}

SAFE_RANGES: dict[str, tuple[float, float]] = {
    "strategy.buy_threshold": (1.0, 4.0),           # hard cap: never block direction
    "strategy.adx_min": (15.0, 35.0),
    "strategy.adx_min_short_offset": (0.0, 10.0),
    "strategy.vol_min": (1.0, 3.0),
    "strategy.trend_min_spread_pct": (0.02, 0.20),
    "risk.position_pct": (0.05, 0.20),
    "risk.max_total_pct": (0.30, 0.70),
    "risk.max_positions": (3.0, 30.0),
    "risk.cooldown_seconds": (1800.0, 21600.0),
    "risk.max_consecutive_losses": (2.0, 5.0),
    "risk.pause_minutes": (30.0, 240.0),
}

REGIME_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (3.0, 10.0),
    "take_profit_pct": (5.0, 30.0),
    "trailing_activation": (3.0, 10.0),
    "trailing_callback": (0.5, 4.0),
}

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("TRADINGAGENTS_SCALPER_AI_MODEL", "deepseek-chat")


def _deepseek_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def compute_stats(closes: list[dict]) -> dict:
    """Aggregate closed trades into a compact stat block for the prompt."""
    n = len(closes)
    pnls = [float(c.get("pnl_usdt") or 0.0) for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)

    by_reason: dict[str, list[float]] = defaultdict(list)
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for c in closes:
        by_reason[c.get("reason") or "?"].append(float(c.get("pnl_usdt") or 0.0))
        by_regime[c.get("regime") or "?"].append(float(c.get("pnl_usdt") or 0.0))
        by_symbol[c.get("symbol") or "?"].append(float(c.get("pnl_usdt") or 0.0))

    def summarize(d: dict[str, list[float]]) -> dict[str, dict]:
        out = {}
        for k, v in d.items():
            out[k] = {
                "n": len(v), "pnl": round(sum(v), 2),
                "avg": round(sum(v) / len(v), 3) if v else 0.0,
            }
        return out

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100.0, 1) if n else 0.0,
        "total_pnl": round(total, 2),
        "avg_pnl": round(total / n, 3) if n else 0.0,
        "best": round(max(pnls), 2) if pnls else 0.0,
        "worst": round(min(pnls), 2) if pnls else 0.0,
        "by_reason": summarize(by_reason),
        "by_regime": summarize(by_regime),
        "by_symbol": summarize(by_symbol),
    }


def current_effective_params(store: ScalperStore) -> dict[str, float]:
    """Defaults merged with the latest applied AI config (so consecutive runs
    see the active values, not the factory defaults)."""
    params = dict(DEFAULT_PARAMS)
    raw = store.get_state("ai_config", "")
    if raw:
        try:
            cfg = json.loads(raw)
            for path, val in (cfg.get("params") or {}).items():
                if path in params:
                    params[path] = float(val)
        except (ValueError, TypeError):
            pass
    # Regime params are keyed regime.symbol.param
    for regime, syms in REGIME_PARAMS.items():
        for sym, kv in syms.items():
            for k, v in kv.items():
                params[f"regime.{regime}.{sym}.{k}"] = float(v)
    return params


def _param_default(path: str) -> float | None:
    if path in DEFAULT_PARAMS:
        return DEFAULT_PARAMS[path]
    parts = path.split(".")
    if len(parts) == 4 and parts[0] == "regime":
        regime, sym, key = parts[1], parts[2], parts[3]
        return REGIME_PARAMS.get(regime, {}).get(sym, {}).get(key)
    return None


def _param_range(path: str) -> tuple[float, float] | None:
    if path in SAFE_RANGES:
        return SAFE_RANGES[path]
    parts = path.split(".")
    if len(parts) == 4 and parts[0] == "regime":
        return REGIME_PARAM_RANGES.get(parts[3])
    return None


def validate_and_clamp(adjustments: list[dict], current: dict[str, float]) -> tuple[list[dict], list[str]]:
    """Clamp every adjustment to [min,max] and ±50% of its default.

    Returns (approved, warnings). Unknown paths are dropped.
    """
    approved: list[dict] = []
    warnings: list[str] = []
    for adj in adjustments:
        if not isinstance(adj, dict):
            continue
        path = adj.get("param")
        try:
            value = float(adj.get("value"))
        except (TypeError, ValueError):
            warnings.append(f"{path}: 非数值，忽略")
            continue
        rng = _param_range(path)
        default = _param_default(path)
        if rng is None or default is None:
            warnings.append(f"{path}: 不在可调参数表内，忽略")
            continue
        lo, hi = rng
        # ±50% of default, then absolute range
        lo = max(lo, default * 0.5)
        hi = min(hi, default * 1.5)
        clamped = max(lo, min(hi, value))
        if clamped != value:
            warnings.append(f"{path}: {value} 越界，clamp 到 {clamped}")
        approved.append({
            "param": path,
            "value": round(clamped, 4),
            "reason": str(adj.get("reason", ""))[:200],
        })
    return approved, warnings


def call_deepseek(prompt: str, system: str) -> str | None:
    """Call DeepSeek chat completions (domestic direct, no proxy)."""
    key = _deepseek_key()
    if not key:
        logger.warning("DEEPSEEK_API_KEY 未配置，跳过 AI 调参")
        return None
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2000,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.error("DeepSeek 调用失败: %s", exc)
        return None


def parse_adjustments(text: str | None) -> list[dict]:
    """Parse the model output. Expects JSON object with ``adjustments`` list,
    falls back to a bare JSON array."""
    if not text:
        return []
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            return obj.get("adjustments") or []
    except ValueError:
        pass
    # Last resort: extract the first [...] block.
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return []
    return []


def build_prompt(stats: dict, current: dict[str, float], panic: bool) -> str:
    lines = [
        "你是专业的加密货币杠杆交易策略调参师。",
        "系统：OKX 模拟盘 5x 杠杆 15 分钟趋势策略（BTC/ETH/SOL），名义资金 160 USDT，双向交易。",
        "",
        f"最近 {stats['n']} 笔平仓统计：",
        f"- 胜率 {stats['win_rate']}%（赢 {stats['wins']} / 亏 {stats['losses']}）",
        f"- 总盈亏 {stats['total_pnl']:+.2f} USDT，平均每笔 {stats['avg_pnl']:+.3f}，最好 {stats['best']:+.2f}，最差 {stats['worst']:+.2f}",
        f"- 按平仓原因：{json.dumps(stats['by_reason'], ensure_ascii=False)}",
        f"- 按市场状态：{json.dumps(stats['by_regime'], ensure_ascii=False)}",
        f"- 按币种：{json.dumps(stats['by_symbol'], ensure_ascii=False)}",
        "",
        "当前参数（路径 → 当前值）：",
    ]
    for path, val in sorted(current.items()):
        lines.append(f"- {path} = {val}")
    lines += [
        "",
        "请根据统计数据判断哪些参数需要调整。规则：",
        "1. 只调整有明确数据支撑的参数；没有把握就返回空数组 []",
        "2. 输出 JSON 对象：{\"adjustments\": [{\"param\": \"路径\", \"value\": 数值, \"reason\": \"中文理由\"}]}",
        "3. 参数路径必须严格等于上面列表中的路径",
        "4. 每个参数最多调整一次，调整幅度不超过当前值的 30%",
    ]
    if panic:
        lines += [
            "5. ⚠️ 当前处于亏损状态：只允许缩小仓位(risk.position_pct)、提高入场门槛(strategy.buy_threshold)，"
            "禁止放宽止损(regime.*.stop_loss_pct)，禁止调整 take_profit 向下",
        ]
    lines += [
        "6. 永续合约禁止暂停交易、禁止把 buy_threshold 调到 4.0 以上",
        "7. 只输出 JSON，不要输出任何其他文字",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="杠杆 scalper DeepSeek 周期调参")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入 store 不发飞书")
    parser.add_argument("--min-trades", type=int, default=3, help="距上次调参最少平仓笔数（默认3）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db_path = os.environ.get(
        "TRADINGAGENTS_SCALPER_DB_PATH",
        "/root/.tradingagents/cache/runner_scalper.db",
    )
    store = ScalperStore(db_path)

    # Only act when enough NEW closes have accumulated since the last run.
    raw = store.get_state("ai_config", "")
    last_ts = 0.0
    if raw:
        try:
            last_ts = float(json.loads(raw).get("version", 0.0))
        except (ValueError, TypeError):
            pass
    closes = store.closes_since(last_ts)
    logger.info("距上次调参新增平仓 %d 笔（阈值 %d）", len(closes), args.min_trades)
    if len(closes) < args.min_trades:
        logger.info("交易样本不足，跳过本轮")
        return 0

    stats = compute_stats(closes)
    current = current_effective_params(store)
    panic = stats["win_rate"] < 30.0 or store.consecutive_losses() >= 3
    prompt = build_prompt(stats, current, panic)

    system = "你是加密货币交易策略调参师，只输出 JSON。"
    logger.info("调用 DeepSeek 调参分析（%d 笔样本，panic=%s）...", stats["n"], panic)
    text = call_deepseek(prompt, system)
    if text is None:
        return 1

    raw_adjs = parse_adjustments(text)
    approved, warnings = validate_and_clamp(raw_adjs, current)

    version = time.time()
    summary = f"胜率{stats['win_rate']}% 总盈亏{stats['total_pnl']:+.2f}"
    for w in warnings:
        logger.warning("校验: %s", w)

    if not approved:
        logger.info("AI 无参数调整，记录本轮分析（version=%.0f）", version)
        if not args.dry_run:
            store.set_state("ai_config", json.dumps(
                {"version": version, "params": {}, "summary": summary}, ensure_ascii=False))
            store.record_ai_log(summary, {}, source="no-change")
        return 0

    lines = [f"📊 样本: {stats['n']}笔 | {summary}", "调参:"]
    for a in approved:
        lines.append(f"  • {a['param']}: {current.get(a['param'], '?')} → {a['value']}（{a['reason']}）")
    for w in warnings:
        lines.append(f"  ⚠️ {w}")
    msg = "\n".join(lines)
    print(msg)

    if args.dry_run:
        logger.info("dry-run 模式，不写入")
        return 0

    store.set_state("ai_config", json.dumps(
        {"version": version, "params": {a["param"]: a["value"] for a in approved},
         "summary": summary}, ensure_ascii=False))
    store.record_ai_log(summary, {a["param"]: a["value"] for a in approved}, source="deepseek")
    feishu_send(f"[杠杆AI] 自动调参\n{msg}")
    logger.info("已写入 ai_config（version=%.0f），运行中循环 60s 内热加载", version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
