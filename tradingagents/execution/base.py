"""Broker adapter interface shared by all execution backends.

Concrete brokers (``CryptoBroker`` for ccxt spot crypto, future stock brokers
for Alpaca/IBKR) implement ``place_order``. Keeping the interface narrow lets
``trading_graph`` call any backend without knowing whether it talks to a REST
API, a FIX session, or an in-memory paper matcher.
"""

from __future__ import annotations

from typing import Any

from tradingagents.agents.schemas import PortfolioDecision


class OrderResult(dict):
    """Structured result of an order attempt.

    Keys:
        status:   "filled" | "skipped" | "error"
        reason:   human-readable explanation when not filled
        order:    raw broker payload on success (exchange-specific)
        action:   "BUY" | "SELL" | "HOLD" that was attempted
        symbol:   ccxt-style market symbol the order targeted
        price:    fill / quote price when known
        amount:   base-asset quantity when known
    """

    @property
    def status(self) -> str:
        return self.get("status", "error")

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"


class BaseBroker:
    """Abstract broker: map a PortfolioDecision to an exchange order.

    Subclasses implement ``place_order``. All brokers must be safe to
    construct with missing credentials in paper mode (testnets commonly
    accept throwaway keys) and must refuse to trade in live mode without
    real credentials.
    """

    def place_order(
        self,
        symbol: str,
        decision: PortfolioDecision,
        **kwargs: Any,
    ) -> OrderResult:
        raise NotImplementedError
