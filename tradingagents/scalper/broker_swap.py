"""OKX SWAP execution layer for the scalper (ccxt, paper/sandbox only).

Re-implements the legacy scalp-trader futures client on top of ccxt so it
shares the project's existing proxy/credentials plumbing. Key differences
from the spot broker (``tradingagents.execution.crypto_broker``):

- Markets are perpetual swaps (``BTC/USDT:USDT``), not spot.
- Hedge mode: long and short positions can coexist per symbol.
- Orders carry ``posSide`` + ``tdMode=cross``; closing is ``reduceOnly``.
- TP/SL are attached at open time via ``attachAlgoOrds`` (mark-price trigger,
  market execution) — same behaviour as the legacy client.
- Contracts are integer lots: ``ctVal`` per lot (BTC=0.01, ETH=0.1, SOL=1),
  notional = lots * ctVal * price.

Safety: this class is hard-wired to sandbox mode (``set_sandbox_mode(True)``)
and refuses to construct unless ``sandbox=True``. It is a paper-trading
executor; no real funds path exists here.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SwapBrokerError(RuntimeError):
    """Raised when the exchange rejects an operation."""


class SwapBroker:
    """OKX swap broker for the scalper (hedge mode, cross margin)."""

    def __init__(
        self,
        exchange: Any,
        leverage: int = 5,
        sandbox: bool = True,
    ) -> None:
        if not sandbox:
            raise ValueError(
                "SwapBroker only supports sandbox (paper) trading; refusing live mode."
            )
        self.exchange = exchange
        self.leverage = int(leverage)
        self.sandbox = sandbox
        # OKX sandbox must be enabled before any authenticated call.
        if not getattr(self.exchange, "sandbox_mode", False):
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("set_sandbox_mode failed (continuing): %s", exc)

    # ------------------------------------------------------------------ #
    # Setup
    # ------------------------------------------------------------------ #

    def ensure_hedge_mode(self) -> None:
        """Put the account in long/short hedge mode (idempotent)."""
        try:
            self.exchange.set_position_mode(True)
            logger.info("swap account set to hedge mode (long_short_mode)")
        except Exception as exc:
            raise SwapBrokerError(f"set_position_mode failed: {exc}") from exc

    def set_leverage(self, symbol: str, pos_side: str = "long") -> None:
        """Set cross-margin leverage for one side of a swap market."""
        try:
            self.exchange.set_leverage(
                self.leverage,
                symbol,
                params={"mgnMode": "cross", "posSide": pos_side},
            )
        except Exception as exc:
            raise SwapBrokerError(
                f"set_leverage({symbol},{pos_side},{self.leverage}x) failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def contract_size(self, symbol: str) -> float:
        """Per-lot contract value (ctVal) for a swap market."""
        market = self.exchange.market(symbol)
        return float(market.get("contractSize") or market.get("info", {}).get("ctVal") or 1.0)

    def fetch_price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        last = ticker.get("last")
        if not last:
            raise SwapBrokerError(f"no last price for {symbol}")
        return float(last)

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 80) -> list[list[float]]:
        """OHLCV rows as [[ts, open, high, low, close, volume], ...]."""
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [[float(x) for x in row] for row in rows]

    def fetch_equity(self) -> float:
        balance = self.exchange.fetch_balance()
        total = balance.get("total", {})
        return float(total.get("USDT", 0.0) or 0.0)

    def fetch_positions(self) -> list[dict[str, Any]]:
        """Open swap positions (hedge mode) as dicts with unified keys."""
        raw = self.exchange.fetch_positions()
        out: list[dict[str, Any]] = []
        for pos in raw:
            contracts = float(pos.get("contracts") or 0.0)
            if contracts == 0:
                continue
            out.append(
                {
                    "symbol": pos.get("symbol"),
                    "side": pos.get("side"),  # "long" | "short"
                    "contracts": contracts,
                    "entry_price": float(pos.get("entryPrice") or 0.0),
                    "notional": float(pos.get("notional") or 0.0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl") or 0.0),
                    "leverage": float(pos.get("leverage") or self.leverage),
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Order helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def calc_contracts(usdt_margin: float, price: float, ct_val: float) -> int:
        """Lots = margin / (price * ctVal), minimum 1.

        Matches the legacy strategy's (conservative) sizing — the comment in
        the old code claimed leverage was included, the code did not; we keep
        the actual behaviour that was running and validated on paper.
        """
        if price <= 0 or ct_val <= 0 or usdt_margin <= 0:
            return 0
        contracts = usdt_margin / (price * ct_val)
        return max(1, int(contracts))

    def open_position(
        self,
        symbol: str,
        side: str,  # "long" | "short"
        contracts: int,
        price: float,
        sl_pct: float,
        tp_pct: float,
    ) -> dict[str, Any]:
        """Open a market swap position with attached TP/SL.

        ``side`` is the *position* side (long/short); the exchange order
        side is buy for long, sell for short. TP/SL trigger prices are
        derived from the current price and attached via ``attachAlgoOrds``
        so the exchange enforces them even if this process dies.
        """
        if contracts <= 0:
            raise SwapBrokerError("contracts must be > 0")
        pos_side = side
        order_side = "buy" if side == "long" else "sell"
        if side == "long":
            sl_px, tp_px = price * (1 - sl_pct / 100.0), price * (1 + tp_pct / 100.0)
        else:
            sl_px, tp_px = price * (1 + sl_pct / 100.0), price * (1 - tp_pct / 100.0)

        attach = [
            {"slTriggerPx": f"{sl_px:.2f}", "slOrdPx": "-1", "slTriggerPxType": "mark"},
            {"tpTriggerPx": f"{tp_px:.2f}", "tpOrdPx": "-1", "tpTriggerPxType": "mark"},
        ]
        order = self.exchange.create_order(
            symbol,
            "market",
            order_side,
            contracts,
            params={
                "tdMode": "cross",
                "posSide": pos_side,
                "attachAlgoOrds": attach,
            },
        )
        logger.info(
            "swap OPEN %s %s %s lots @ ~%.2f (sl=%.2f tp=%.2f) id=%s",
            side.upper(), symbol, contracts, price, sl_px, tp_px,
            order.get("id"),
        )
        return {
            "side": side,
            "contracts": contracts,
            "entry_price": price,
            "order_id": order.get("id"),
            "sl_px": sl_px,
            "tp_px": tp_px,
        }

    def close_position(self, symbol: str, side: str, contracts: int) -> dict[str, Any]:
        """Close a position (reduceOnly) at market."""
        order_side = "sell" if side == "long" else "buy"
        order = self.exchange.create_order(
            symbol,
            "market",
            order_side,
            contracts,
            params={"tdMode": "cross", "posSide": side, "reduceOnly": True},
        )
        logger.info("swap CLOSE %s %s %s lots id=%s", side.upper(), symbol, contracts, order.get("id"))
        return {"side": side, "contracts": contracts, "order_id": order.get("id")}
