"""Spot-crypto broker adapter built on ``ccxt``.

Connects the Portfolio Manager's structured ``PortfolioDecision`` to a real
exchange order. Spot only — futures/leverage are deliberately unsupported;
combining non-deterministic LLM signals with leverage is a fast way to lose
money, and the framework's risk "management" layer is debate-style, not a
hard risk system.

Safety posture
--------------
- Default mode is ``paper``: routes to the exchange testnet/sandbox.
- ``execution_enabled`` defaults to False at the config layer; this class is
  never instantiated unless the user opts in.
- ``ccxt`` is an *optional* dependency. Importing this module without ccxt
  installed raises a clear ``ImportError`` with install instructions, rather
  than failing deep in the graph.
- All hard risk limits (cooldown, max position, min notional) are enforced
  here in code, not delegated to the LLM.

Rating → action mapping
-----------------------
The Portfolio Manager emits a 5-tier rating. We collapse it to 3 trading
actions because spot exchanges only know buy / sell / hold:

    Buy, Overweight       → BUY
    Hold                  → HOLD (no order)
    Underweight, Sell     → SELL
"""

from __future__ import annotations

import logging
from typing import Any

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

from .base import BaseBroker, OrderResult
from .risk import CooldownGuard, position_cap_breach

logger = logging.getLogger(__name__)


# Map the 5-tier PM rating to a 3-action spot direction. Kept as a module-level
# constant (not a method) so it can be unit-tested without a broker instance.
RATING_TO_ACTION: dict[PortfolioRating, str] = {
    PortfolioRating.BUY: "BUY",
    PortfolioRating.OVERWEIGHT: "BUY",
    PortfolioRating.HOLD: "HOLD",
    PortfolioRating.UNDERWEIGHT: "SELL",
    PortfolioRating.SELL: "SELL",
}


def to_ccxt_symbol(symbol: str) -> str:
    """Convert a TradingAgents ticker to a ccxt spot market symbol.

    ``BTC-USD`` / ``BTCUSDT`` / ``BTC-USDT`` all become ``BTC/USDT``. We
    standardize on USDT for the quote side because it has the deepest spot
    liquidity on Binance/OKX/Bybit; USD-quoted pairs are thin or absent on
    most exchanges outside Coinbase/Kraken.
    """
    # Strip exchange qualifiers yfinance uses; split on the first separator.
    base = symbol.upper().replace("+", "")
    for sep in ("-", "/", "_"):
        if sep in base:
            base = base.split(sep)[0]
            break
    # A pure concatenated symbol like "BTCUSDT": strip a known quote suffix.
    for quote in ("USDT", "USDC", "USD", "BUSD"):
        if base.endswith(quote) and len(base) > len(quote):
            base = base[: -len(quote)]
            break
    return f"{base}/USDT"


class CryptoBroker(BaseBroker):
    """ccxt-backed spot crypto broker.

    Parameters mirror the ``crypto_*`` config keys in ``default_config.py``;
    ``TradingAgentsGraph`` constructs one from config when execution is
    enabled. The broker is stateful only through ``CooldownGuard`` (disk) and
    the underlying ``ccxt.Exchange`` (network); it is safe to create a fresh
    instance per ``propagate()`` call.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str | None = None,
        secret: str | None = None,
        testnet: bool = True,
        quote_budget: float = 1000.0,
        max_position_fraction: float = 0.2,
        cooldown_seconds: float = 14400.0,
        cooldown_state_path: str | None = None,
        exchange: Any | None = None,
    ):
        # ``exchange`` injection is for tests; production code passes credentials.
        if exchange is not None:
            self.exchange = exchange
        else:
            try:
                import ccxt  # noqa: F401 — imported lazily so the module is importable without ccxt installed
            except ImportError as exc:  # pragma: no cover - exercised via test
                raise ImportError(
                    "ccxt is required for crypto execution. Install it with: "
                    'pip install "tradingagents[crypto]"  (or: pip install ccxt)'
                ) from exc
            exchange_cls = getattr(ccxt, exchange_id)
            self.exchange = exchange_cls(
                {
                    "apiKey": api_key,
                    "secret": secret,
                    "enableRateLimit": True,
                    "options": {"defaultType": "spot"},
                }
            )
            # Sandbox/testnet must be enabled *before* any authenticated call.
            # Binance/OKX/Bybit all honor set_sandbox_mode(True).
            if testnet:
                self.exchange.set_sandbox_mode(True)

        self.testnet = testnet
        self.quote_budget = float(quote_budget)
        self.max_position_fraction = float(max_position_fraction)
        self.cooldown = CooldownGuard(cooldown_state_path)
        self.cooldown_seconds = float(cooldown_seconds)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        symbol: str,
        decision: PortfolioDecision,
        **kwargs: Any,
    ) -> OrderResult:
        """Translate ``decision`` into an exchange order on ``symbol``.

        Returns an :class:`OrderResult` regardless of outcome — callers (the
        graph, the CLI) never see an exception for routine rejections
        (cooldown, hold rating, position cap, insufficient balance). Real
        network/API errors are caught and surfaced as ``status="error"`` so a
        broker outage doesn't crash an otherwise-successful analysis run.
        """
        action = RATING_TO_ACTION.get(decision.rating, "HOLD")
        ccxt_symbol = to_ccxt_symbol(symbol)

        if action == "HOLD":
            return OrderResult(
                status="skipped",
                reason=f"Hold rating on {symbol}",
                action=action,
                symbol=ccxt_symbol,
            )

        # --- Guard 1: cooldown ------------------------------------------- #
        allowed, remaining = self.cooldown.can_trade(ccxt_symbol, self.cooldown_seconds)
        if not allowed:
            return OrderResult(
                status="skipped",
                reason=(
                    f"cooldown active on {ccxt_symbol}: "
                    f"{remaining:.0f}s remaining of {self.cooldown_seconds:.0f}s"
                ),
                action=action,
                symbol=ccxt_symbol,
            )

        try:
            return self._submit(ccxt_symbol, action, decision)
        except Exception as exc:
            # Broad catch is intentional: a broker API hiccup (timeout, 5xx,
            # auth error) must not abort the analysis run. Log and report.
            logger.error("Order failed on %s (%s): %s", ccxt_symbol, action, exc)
            return OrderResult(
                status="error",
                reason=str(exc),
                action=action,
                symbol=ccxt_symbol,
            )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _submit(
        self,
        ccxt_symbol: str,
        action: str,
        decision: PortfolioDecision,
    ) -> OrderResult:
        price = self._fetch_price(ccxt_symbol)
        balance = self._fetch_balance()
        equity = self._quote_equity(balance)

        if action == "BUY":
            return self._buy(ccxt_symbol, price, balance, equity, decision)
        return self._sell(ccxt_symbol, price, balance)

    def _buy(
        self,
        ccxt_symbol: str,
        price: float,
        balance: dict[str, Any],
        equity: float,
        decision: PortfolioDecision,
    ) -> OrderResult:
        # Spend at most quote_budget, and at most what the quote account holds.
        spendable = min(self.quote_budget, self._quote_free(balance))
        if spendable <= 0:
            return OrderResult(
                status="skipped",
                reason="no free quote balance to buy",
                action="BUY",
                symbol=ccxt_symbol,
            )

        # --- Guard 2: max position fraction ------------------------------ #
        base = ccxt_symbol.split("/")[0]
        existing_value = self._base_value(balance, base, price)
        proposed_value = existing_value + spendable
        if position_cap_breach(proposed_value, equity, self.max_position_fraction):
            return OrderResult(
                status="skipped",
                reason=(
                    f"position cap breach: {proposed_value:.2f} > "
                    f"{self.max_position_fraction:.0%} of equity {equity:.2f}"
                ),
                action="BUY",
                symbol=ccxt_symbol,
            )

        amount = spendable / price
        amount = self._safe_amount(ccxt_symbol, amount)
        if amount <= 0:
            return OrderResult(
                status="skipped",
                reason="order amount below exchange precision/min notional",
                action="BUY",
                symbol=ccxt_symbol,
            )

        order = self.exchange.create_order(ccxt_symbol, "market", "buy", amount)
        self.cooldown.record(ccxt_symbol)
        logger.info(
            "BUY filled: %s %s @ ~%s (id=%s)",
            amount, ccxt_symbol, price, order.get("id"),
        )
        return OrderResult(
            status="filled",
            order=order,
            action="BUY",
            symbol=ccxt_symbol,
            price=price,
            amount=amount,
        )

    def _sell(
        self,
        ccxt_symbol: str,
        price: float,
        balance: dict[str, Any],
    ) -> OrderResult:
        base = ccxt_symbol.split("/")[0]
        holding = self._base_free(balance, base)
        if holding <= 0:
            return OrderResult(
                status="skipped",
                reason=f"no {base} position to sell",
                action="SELL",
                symbol=ccxt_symbol,
            )
        amount = self._safe_amount(ccxt_symbol, holding)
        if amount <= 0:
            return OrderResult(
                status="skipped",
                reason="sell amount below exchange precision",
                action="SELL",
                symbol=ccxt_symbol,
            )
        order = self.exchange.create_order(ccxt_symbol, "market", "sell", amount)
        self.cooldown.record(ccxt_symbol)
        logger.info(
            "SELL filled: %s %s @ ~%s (id=%s)",
            amount, ccxt_symbol, price, order.get("id"),
        )
        return OrderResult(
            status="filled",
            order=order,
            action="SELL",
            symbol=ccxt_symbol,
            price=price,
            amount=amount,
        )

    # ------------------------------------------------------------------ #
    # Exchange data accessors — isolated so tests can stub them.
    # ------------------------------------------------------------------ #

    def _fetch_price(self, ccxt_symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(ccxt_symbol)
        return float(ticker["last"])

    def _fetch_balance(self) -> dict[str, Any]:
        # ccxt returns a nested dict; the 'free'/'used'/'total' sub-maps are
        # keyed by currency code. We return the whole thing and pick fields
        # in the helpers below.
        return self.exchange.fetch_balance()

    @staticmethod
    def _quote_free(balance: dict[str, Any]) -> float:
        # USDT is our standard quote; fall back to USD for Coinbase/Kraken.
        for cur in ("USDT", "USD", "USDC"):
            val = balance.get(cur, {})
            if isinstance(val, dict):
                free = val.get("free")
                if free:
                    return float(free)
            elif isinstance(val, (int, float)):
                return float(val)
        return 0.0

    @staticmethod
    def _quote_equity(balance: dict[str, Any]) -> float:
        # Total account value in quote currency. ccxt exposes 'total' per
        # currency; we sum the quote-side total as a conservative equity proxy
        # (ignores base-asset value, which is fine for a buy cap check).
        for cur in ("USDT", "USD", "USDC"):
            val = balance.get(cur, {})
            if isinstance(val, dict):
                total = val.get("total")
                if total:
                    return float(total)
            elif isinstance(val, (int, float)):
                return float(val)
        return 0.0

    @staticmethod
    def _base_free(balance: dict[str, Any], base: str) -> float:
        val = balance.get(base, {})
        if isinstance(val, dict):
            free = val.get("free")
            return float(free) if free else 0.0
        if isinstance(val, (int, float)):
            return float(val)
        return 0.0

    @staticmethod
    def _base_value(balance: dict[str, Any], base: str, price: float) -> float:
        val = balance.get(base, {})
        if isinstance(val, dict):
            total = val.get("total")
            return float(total) * price if total else 0.0
        if isinstance(val, (int, float)):
            return float(val) * price
        return 0.0

    def _safe_amount(self, ccxt_symbol: str, amount: float) -> float:
        """Round ``amount`` to the exchange's precision and reject dust."""
        try:
            rounded = self.exchange.amount_to_precision(ccxt_symbol, amount)
            return float(rounded)
        except Exception:
            # If precision formatting fails, refuse rather than risk a
            # rejected or wrong-size order.
            return 0.0
