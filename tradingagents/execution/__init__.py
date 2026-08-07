"""Optional order-execution layer for TradingAgents.

The framework's default behavior is analysis-only: it produces a
``PortfolioDecision`` and writes reports. This package adds the final hop —
translating that decision into a real broker order — behind an opt-in config
flag (``execution_enabled``). When disabled, no code in this package runs and
no orders are ever placed.

Currently ships a single spot-crypto broker (``crypto_broker``) built on
``ccxt``. Stock-broker adapters can follow the same ``BaseBroker`` interface.
"""

from __future__ import annotations

from .base import BaseBroker, OrderResult
from .crypto_broker import CryptoBroker
from .risk import CooldownGuard

__all__ = [
    "BaseBroker",
    "CryptoBroker",
    "CooldownGuard",
    "OrderResult",
]
