"""High-frequency scalper loop (independent process, paper trading).

Runs in its own process via ``run_scalper.py`` and never touches the AI
runner. Responsibilities per tick:

1. Refresh market regime (BTC K-line, cached 30 min).
2. For each symbol: manage open positions (stop-loss / take-profit /
   trailing), then evaluate entry signals.
3. Enforce cooldowns, max positions, consecutive-loss pause.
4. Persist everything to the scalper's own SQLite store.
5. Push trade events to Feishu with a ``[杠杆]`` prefix.

Entry sizing follows the legacy strategy: margin = available * position_pct
(10%), capped by max_total_pct (50%) of capital; lots computed by the broker
(margin / (price * ctVal), min 1). 5x leverage is set on the exchange.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

from .broker_swap import SwapBroker
from .market_regime import MarketRegime, get_params
from .state_store import ScalperStore
from .strategy import ScalpStrategy, ScoreResult

logger = logging.getLogger(__name__)

_FEISHU_WEBHOOK = os.environ.get("TRADINGAGENTS_FEISHU_WEBHOOK", "")


def feishu_send(text: str) -> None:
    """Send a Feishu text notification (silent failure)."""
    if not _FEISHU_WEBHOOK:
        return
    try:
        payload = json.dumps({
            "msg_type": "text",
            "content": {"text": text},
        }).encode("utf-8")
        req = urllib.request.Request(
            _FEISHU_WEBHOOK,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001 - notification must never crash the loop
        logger.warning("feishu notify failed: %s", exc)


class ScalperConfig:
    """Runtime knobs (mirror legacy TradingConfig, all adjustable)."""

    def __init__(self, **overrides: Any) -> None:
        self.symbols: dict[str, dict[str, Any]] = {
            "BTC-USDT-SWAP": {"bar": "15m", "kline_count": 80},
            "ETH-USDT-SWAP": {"bar": "15m", "kline_count": 80},
            "SOL-USDT-SWAP": {"bar": "15m", "kline_count": 80},
        }
        self.position_pct = 0.10          # margin per entry (fraction of free capital)
        self.max_total_pct = 0.50         # total margin cap (fraction of capital)
        self.max_positions = 20           # paper: generous; live would be 2-3
        self.cooldown_seconds = 5400.0    # 90 min between entries per symbol
        self.max_consecutive_losses = 3
        self.pause_minutes = 90
        self.fee_rate = 0.001
        self.leverage = 5
        self.notional_capital = 160.0     # 名义资金：按实盘 160 USDT 规模模拟（不用模拟盘余额）
        self.interval_seconds = 5.0
        self.regime_cache_seconds = 1800.0
        for k, v in overrides.items():
            setattr(self, k, v)


class ScalperLoop:
    def __init__(
        self,
        broker: SwapBroker,
        store: ScalperStore,
        config: ScalperConfig | None = None,
        strategy: ScalpStrategy | None = None,
    ) -> None:
        self.broker = broker
        self.store = store
        self.config = config or ScalperConfig()
        self.strategy = strategy or ScalpStrategy()
        self.regime = MarketRegime(cache_seconds=self.config.regime_cache_seconds)
        self.ct_val_cache: dict[str, float] = {}
        self.lot_sz_cache: dict[str, float] = {}
        self.ai_overrides: dict[str, dict[str, dict[str, float]]] = {}
        self._ai_loaded_at = 0.0

    # ------------------------------------------------------------------ #
    # AI-driven hot reload
    # ------------------------------------------------------------------ #

    def refresh_ai_config(self, force: bool = False) -> None:
        """Hot-apply DeepSeek adjustments written to the store (checked at
        most every 60s so the 5s loop stays cheap).

        Two namespaces:
        - ``strategy.*`` / ``risk.*``  → live config objects
        - ``regime.<regime>.<symbol>.<key>`` → get_params overrides
        """
        now = time.time()
        if not force and now - self._ai_loaded_at < 60.0:
            return
        self._ai_loaded_at = now
        raw = self.store.get_state("ai_config", "")
        if not raw:
            return
        try:
            cfg = json.loads(raw)
        except ValueError:
            return
        params = cfg.get("params") or {}
        if not params:
            return

        strategy_cfg = self.strategy.config
        overrides: dict[str, dict[str, dict[str, float]]] = {}
        changed: list[str] = []
        for path, val in params.items():
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            if path.startswith("strategy."):
                attr = path.split(".", 1)[1]
                if hasattr(strategy_cfg, attr):
                    setattr(strategy_cfg, attr, val)
                    changed.append(path)
            elif path.startswith("risk."):
                attr = path.split(".", 1)[1]
                if hasattr(self.config, attr):
                    setattr(self.config, attr, val)
                    changed.append(path)
            elif path.startswith("regime."):
                _, regime, sym, key = path.split(".", 3)
                overrides.setdefault(regime, {}).setdefault(sym, {})[key] = val
                changed.append(path)
        if changed:
            self.ai_overrides = overrides
            logger.info("AI 调参热加载: %s", ", ".join(changed))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _symbol_to_ccxt(self, inst_id: str) -> str:
        """'BTC-USDT-SWAP' → 'BTC/USDT:USDT'."""
        base, quote = inst_id.split("-")[0], inst_id.split("-")[1]
        return f"{base}/{quote}:{quote}"

    def _calc_margin(self, equity: float, open_positions: list[str]) -> float:
        total_used = 0.0
        for symbol in open_positions:
            pos = self.store.get_position(symbol)
            if pos:
                total_used += pos["margin"]
        available = equity - total_used
        margin = available * self.config.position_pct
        max_margin = equity * self.config.max_total_pct - total_used
        return min(margin, max_margin)

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_position(
        self,
        symbol: str,
        pos: dict,
        price: float,
        result: ScoreResult,
    ) -> tuple[bool, str]:
        """Check stop-loss / take-profit / trailing for an open position.

        Returns (should_close, reason).
        """
        entry = pos["entry_price"]
        contracts = pos["contracts"]
        params = pos.get("params", {})

        if pos["side"] == "long":
            pct = (price - entry) / entry * 100.0
            pos["highest"] = max(pos["highest"], price)
        else:
            pct = (entry - price) / entry * 100.0
            pos["lowest"] = min(pos["lowest"], price)

        sl = params.get("stop_loss_pct", 6.0)
        tp = params.get("take_profit_pct", 10.0)
        ta = params.get("trailing_activation", 6.0)
        tc = params.get("trailing_callback", 1.5)

        if pct <= -sl:
            return True, f"止损({pct:.2f}%)"
        if pct >= tp:
            return True, f"止盈({pct:.2f}%)"
        if pct >= ta:
            if pos["side"] == "long":
                trail_stop = pos["highest"] * (1 - tc / 100.0)
                if price <= trail_stop:
                    return True, "移动止盈"
            else:
                trail_stop = pos["lowest"] * (1 + tc / 100.0)
                if price >= trail_stop:
                    return True, "移动止盈"
        return False, ""

    def _close_position(self, symbol: str, pos: dict, price: float, reason: str, regime: str) -> None:
        ccxt_symbol = self._symbol_to_ccxt(symbol)
        try:
            self.broker.close_position(ccxt_symbol, pos["side"], float(pos["contracts"]))
        except Exception as exc:  # noqa: BLE001
            logger.error("close failed for %s: %s", symbol, exc)
            return
        entry = pos["entry_price"]
        if pos["side"] == "long":
            pnl_pct = (price - entry) / entry * 100.0
        else:
            pnl_pct = (entry - price) / entry * 100.0
        pnl_usdt = pos["margin"] * pnl_pct / 100.0 * self.config.leverage - pos["margin"] * self.config.fee_rate
        self.store.record_trade({
            "symbol": symbol, "side": pos["side"], "action": "close",
            "price": price, "contracts": pos["contracts"], "margin": pos["margin"],
            "pnl_usdt": round(pnl_usdt, 4), "pnl_pct": round(pnl_pct, 2),
            "reason": reason, "regime": regime,
        })
        self.store.record_trade_result(won=pnl_usdt > 0)
        self.store.delete_position(symbol)
        self.store.set_cooldown(symbol, time.time() + self.config.cooldown_seconds)
        icon = "🟢" if pnl_usdt >= 0 else "🔴"
        feishu_send(
            f"[杠杆] {symbol} 平仓 {pos['side']} {pos['contracts']}张\n"
            f"原因: {reason} | 盈亏: {pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)\n"
            f"当前价: ${price:.2f}"
        )
        logger.info("%s %s: closed %s %s lots @ %.2f (%s) pnl=%+.2f",
                    icon, symbol, pos["side"], pos["contracts"], price, reason, pnl_usdt)

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #

    def _try_open(
        self,
        symbol: str,
        ccxt_symbol: str,
        price: float,
        closes: list[float],
        result: ScoreResult,
        equity: float,
        open_symbols: list[str],
    ) -> None:
        direction = self.strategy.should_open(result, closes)
        if direction is None:
            return
        if len(open_symbols) >= self.config.max_positions:
            logger.info("   ⏸ %s: max positions reached (%d)", symbol, self.config.max_positions)
            return

        margin = self._calc_margin(equity, open_symbols)
        if margin < 5:
            logger.info("   ⏸ %s: margin %.2f < 5, skip", symbol, margin)
            return

        ct_val = self.ct_val_cache.get(symbol)
        if ct_val is None:
            try:
                ct_val = self.broker.contract_size(ccxt_symbol)
                self.ct_val_cache[symbol] = ct_val
            except Exception as exc:  # noqa: BLE001
                logger.error("contract_size failed for %s: %s", symbol, exc)
                return
        lot_sz = self.lot_sz_cache.get(symbol)
        if lot_sz is None:
            try:
                lot_sz = self.broker.lot_size(ccxt_symbol)
                self.lot_sz_cache[symbol] = lot_sz
            except Exception as exc:  # noqa: BLE001
                logger.error("lot_size failed for %s: %s", symbol, exc)
                return
        contracts = self.broker.calc_contracts(margin, price, ct_val, self.config.leverage, lot_sz)
        if contracts <= 0:
            logger.info("   ⏸ %s: margin %.2f 不够 1 张（名义 %.2f），跳过",
                        symbol, margin, price * ct_val)
            return

        regime = self.regime.get()
        params = get_params(symbol, regime, self.ai_overrides)
        try:
            self.broker.set_leverage(ccxt_symbol, pos_side=direction)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_leverage failed for %s (%s): %s", symbol, direction, exc)

        try:
            self.broker.open_position(
                ccxt_symbol, direction, contracts, price,
                sl_pct=params["stop_loss_pct"], tp_pct=params["take_profit_pct"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("open failed for %s: %s", symbol, exc)
            return

        fee = margin * self.config.fee_rate
        pos = {
            "symbol": symbol,
            "side": direction,
            "contracts": contracts,
            "entry_price": price,
            "margin": margin - fee,
            "entry_time": time.time(),
            "highest": price,
            "lowest": price,
            "params": params,
            "signals": result.signals,
        }
        self.store.upsert_position(pos)
        self.store.record_trade({
            "symbol": symbol, "side": direction, "action": "open",
            "price": price, "contracts": contracts, "margin": pos["margin"],
            "reason": f"score={'buy' if direction=='long' else 'sell'}_score",
            "regime": regime,
        })
        icon = "🟢" if direction == "long" else "🔴"
        feishu_send(
            f"[杠杆] {symbol} 开{direction} {contracts}张\n"
            f"价格: ${price:.2f} | 保证金: ${pos['margin']:.2f} | {self.config.leverage}x\n"
            f"止损: {params['stop_loss_pct']}% | 止盈: {params['take_profit_pct']}%\n"
            f"信号: {' / '.join(result.signals[-3:])}"
        )
        logger.info("   %s %s: OPEN %s %s lots @ %.2f margin=%.2f regime=%s signals=%s",
                    icon, symbol, direction.upper(), contracts, price, pos["margin"],
                    regime, result.signals[-3:])

    # ------------------------------------------------------------------ #
    # Main cycle
    # ------------------------------------------------------------------ #

    def run_once(self) -> None:
        """One scan of all symbols (called repeatedly by the loop)."""
        cfg = self.config

        # Hot-apply DeepSeek parameter adjustments (checked <= every 60s).
        try:
            self.refresh_ai_config()
        except Exception as exc:  # noqa: BLE001
            logger.error("refresh_ai_config failed: %s", exc)

        # Pause gate (consecutive losses).
        paused_until = self.store.paused_until()
        if time.time() < paused_until:
            logger.info("⏸ paused (loss streak) until %.0f", paused_until)
            return

        try:
            equity = self.config.notional_capital
            if equity <= 0:
                # 未配置名义资金时回退到账户真实权益
                equity = self.broker.fetch_equity()
        except Exception as exc:  # noqa: BLE001
            logger.error("fetch_equity failed: %s", exc)
            return

        # Regime refresh using BTC K-lines (cached).
        try:
            btc_ccxt = self._symbol_to_ccxt("BTC-USDT-SWAP")
            btc_ohlcv = self.broker.fetch_ohlcv(btc_ccxt, "15m", 60)
            if btc_ohlcv:
                closes = [row[4] for row in btc_ohlcv]
                highs = [row[2] for row in btc_ohlcv]
                lows = [row[3] for row in btc_ohlcv]
                old_regime = self.regime.get()
                regime = self.regime.update(closes, highs, lows)
                if regime != old_regime:
                    self.store.record_regime(regime)
                    logger.info("market regime: %s -> %s", old_regime or "?", regime)
            else:
                regime = self.regime.get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("regime refresh failed: %s", exc)
            regime = self.regime.get()

        open_symbols = [p["symbol"] for p in self.store.all_positions()]

        for symbol, sym_cfg in cfg.symbols.items():
            try:
                self._process_symbol(symbol, sym_cfg, equity, open_symbols, regime)
            except Exception as exc:  # noqa: BLE001
                logger.error("symbol %s processing failed: %s", symbol, exc)

    def _process_symbol(
        self,
        symbol: str,
        sym_cfg: dict[str, Any],
        equity: float,
        open_symbols: list[str],
        regime: str,
    ) -> None:
        # Cooldown.
        remaining = self.store.cooldown_remaining(symbol)
        if remaining > 0:
            logger.debug("   ⏸ %s cooldown %.0fs", symbol, remaining)
            return

        ccxt_symbol = self._symbol_to_ccxt(symbol)
        ohlcv = self.broker.fetch_ohlcv(ccxt_symbol, sym_cfg.get("bar", "15m"), sym_cfg.get("kline_count", 80))
        if not ohlcv:
            return
        candles = [
            {
                "close": row[4], "high": row[2], "low": row[3],
                "open": row[1], "volume": row[5], "timestamp": int(row[0]),
            }
            for row in ohlcv
        ]
        price = self.broker.fetch_price(ccxt_symbol)
        if not candles or not price:
            return

        result = self.strategy.evaluate(candles)
        closes = [c["close"] for c in candles]

        # --- Position management ------------------------------------- #
        pos = self.store.get_position(symbol)
        if pos:
            should_close, reason = self._manage_position(symbol, pos, price, result)
            if should_close:
                self._close_position(symbol, pos, price, reason, regime)
            else:
                pct = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
                if pos["side"] == "short":
                    pct = -pct
                logger.info("   %s %s: %s %s lots @ %.2f | %+.2f%%",
                            "🟢" if pos["side"] == "long" else "🔴", symbol,
                            pos["side"], pos["contracts"], price, pct)
            return

        # --- Entry ----------------------------------------------------- #
        self._try_open(symbol, ccxt_symbol, price, closes, result, equity, open_symbols)


def run_forever(config: ScalperConfig | None = None, broker: SwapBroker | None = None) -> None:
    """Entry point: build the broker/store and loop forever.

    Credentials always come from the dedicated scalper (simulated) env vars
    ``TRADINGAGENTS_SCALPER_*`` — never the live spot keys. If no broker is
    passed, one is built here from those env vars; a missing key is a hard
    error (no silent fallback to live credentials).
    """
    cfg = config or ScalperConfig()

    if broker is None:
        import ccxt

        api_key = os.environ.get("TRADINGAGENTS_SCALPER_API_KEY", "")
        secret = os.environ.get("TRADINGAGENTS_SCALPER_SECRET", "")
        passphrase = os.environ.get("TRADINGAGENTS_SCALPER_PASSPHRASE", "")
        if not (api_key and secret and passphrase):
            raise SystemExit(
                "Refusing to start scalper without explicit simulated credentials. "
                "Set TRADINGAGENTS_SCALPER_API_KEY/SECRET/PASSPHRASE in .env."
            )
        exchange = ccxt.okx({
            "apiKey": api_key,
            "secret": secret,
            "password": passphrase,
            "enableRateLimit": True,
        })
        proxy = os.environ.get("TRADINGAGENTS_SCALPER_PROXY", "http://127.0.0.1:7897")
        if proxy:
            exchange.proxies = {"http": proxy, "https": proxy}
        broker = SwapBroker(exchange, leverage=cfg.leverage, sandbox=True)

    store = ScalperStore(
        os.environ.get("TRADINGAGENTS_SCALPER_DB_PATH", "/root/.tradingagents/cache/runner_scalper.db")
    )
    loop = ScalperLoop(broker, store, cfg)

    # Idempotent account setup.
    try:
        broker.ensure_hedge_mode()
    except Exception as exc:  # noqa: BLE001
        logger.error("hedge mode setup failed: %s", exc)

    logger.info("scalper loop starting: symbols=%s interval=%.1fs leverage=%dx",
                list(cfg.symbols.keys()), cfg.interval_seconds, cfg.leverage)
    feishu_send(f"[杠杆] scalper 启动，{cfg.leverage}x 模拟盘，监控 {', '.join(cfg.symbols)}")

    while True:
        t0 = time.time()
        try:
            loop.run_once()
        except Exception as exc:  # noqa: BLE001
            logger.error("run_once failed: %s", exc)
        elapsed = time.time() - t0
        time.sleep(max(0.1, cfg.interval_seconds - elapsed))
