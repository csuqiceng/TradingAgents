"""Unit tests for the optional crypto execution layer.

These tests never touch the network or require ccxt to be installed: a fake
exchange is injected via ``CryptoBroker(exchange=...)`` so the broker's
decision logic (rating → action, risk guards, balance math) can be verified
deterministically. The ccxt import path itself is covered by a separate
import-error test.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
from tradingagents.execution.crypto_broker import (
    RATING_TO_ACTION,
    CryptoBroker,
    to_ccxt_symbol,
)
from tradingagents.execution.risk import (
    CooldownGuard,
    position_cap_breach,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeExchange:
    """Minimal stand-in for a ccxt exchange used by CryptoBroker.

    Only the methods/attributes the broker touches are implemented. Calling
    any unexpected method surfaces as AttributeError, which is what we want —
    a test that hits an unimplemented method is a signal the broker grew new
    exchange calls that aren't covered.
    """

    def __init__(
        self,
        price: float = 50000.0,
        balances: dict[str, dict[str, float]] | None = None,
        order_id: str = "test-order-1",
        raise_on_create: Exception | None = None,
        precision_amount: float | None = None,
    ):
        self.price = price
        self.balances = balances or {"USDT": {"free": 5000.0, "total": 5000.0}}
        self.order_id = order_id
        self.raise_on_create = raise_on_create
        self.precision_amount = precision_amount
        self.sandbox_enabled = False
        self.created_orders: list[dict[str, Any]] = []

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox_enabled = enabled

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"last": self.price, "symbol": symbol}

    def fetch_balance(self) -> dict[str, Any]:
        return dict(self.balances)

    def create_order(
        self, symbol: str, type_: str, side: str, amount: float
    ) -> dict[str, Any]:
        if self.raise_on_create is not None:
            raise self.raise_on_create
        order = {"id": self.order_id, "symbol": symbol, "type": type_, "side": side, "amount": amount}
        self.created_orders.append(order)
        return order

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        if self.precision_amount is not None:
            rounded = round(amount / self.precision_amount) * self.precision_amount
            return f"{rounded:.8f}".rstrip("0").rstrip(".")
        return f"{amount:.8f}".rstrip("0").rstrip(".")


def _decision(rating: PortfolioRating) -> PortfolioDecision:
    return PortfolioDecision(
        rating=rating, executive_summary="", investment_thesis=""
    )


# ---------------------------------------------------------------------------
# Rating → action mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRatingToAction:
    def test_buy_maps_to_buy(self):
        assert RATING_TO_ACTION[PortfolioRating.BUY] == "BUY"

    def test_overweight_maps_to_buy(self):
        assert RATING_TO_ACTION[PortfolioRating.OVERWEIGHT] == "BUY"

    def test_hold_maps_to_hold(self):
        assert RATING_TO_ACTION[PortfolioRating.HOLD] == "HOLD"

    def test_underweight_maps_to_sell(self):
        assert RATING_TO_ACTION[PortfolioRating.UNDERWEIGHT] == "SELL"

    def test_sell_maps_to_sell(self):
        assert RATING_TO_ACTION[PortfolioRating.SELL] == "SELL"

    def test_every_rating_has_a_mapping(self):
        # Guards against a future rating tier being added without a broker action.
        for rating in PortfolioRating:
            assert rating in RATING_TO_ACTION


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSymbolConversion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("BTC-USD", "BTC/USDT"),
            ("BTC-USDT", "BTC/USDT"),
            ("BTCUSDT", "BTC/USDT"),
            ("btc-usd", "BTC/USDT"),
            ("ETH-USD", "ETH/USDT"),
            ("SOL-USDT", "SOL/USDT"),
        ],
    )
    def test_known_crypto_forms_normalize_to_usdt_pair(self, raw, expected):
        assert to_ccxt_symbol(raw) == expected

    def test_strips_yfinance_plus_qualifier(self):
        assert to_ccxt_symbol("BTC-USD+") == "BTC/USDT"


# ---------------------------------------------------------------------------
# CooldownGuard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCooldownGuard:
    def test_first_order_allowed(self, tmp_path):
        guard = CooldownGuard(tmp_path / "cooldowns.json")
        allowed, remaining = guard.can_trade("BTC/USDT", 3600)
        assert allowed is True
        assert remaining is None

    def test_blocks_within_cooldown_window(self, tmp_path):
        path = tmp_path / "cooldowns.json"
        guard = CooldownGuard(path)
        guard.record("BTC/USDT")
        allowed, remaining = guard.can_trade("BTC/USDT", 3600)
        assert allowed is False
        assert remaining is not None
        assert 0 < remaining <= 3600

    def test_allows_after_window_expires(self, tmp_path):
        path = tmp_path / "cooldowns.json"
        guard = CooldownGuard(path)
        # Manually backdate the last order past the cooldown.
        guard.record("BTC/USDT")
        import time as _time
        state = guard._load()
        state["BTC/USDT"] = _time.time() - 4000
        guard._dump(state)
        allowed, remaining = guard.can_trade("BTC/USDT", 3600)
        assert allowed is True
        assert remaining is None

    def test_independent_symbols_do_not_block_each_other(self, tmp_path):
        guard = CooldownGuard(tmp_path / "cooldowns.json")
        guard.record("BTC/USDT")
        allowed, _ = guard.can_trade("ETH/USDT", 3600)
        assert allowed is True

    def test_zero_cooldown_disables_guard(self, tmp_path):
        guard = CooldownGuard(tmp_path / "cooldowns.json")
        guard.record("BTC/USDT")
        allowed, remaining = guard.can_trade("BTC/USDT", 0)
        assert allowed is True
        assert remaining is None

    def test_corrupt_state_file_fails_open(self, tmp_path):
        path = tmp_path / "cooldowns.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        guard = CooldownGuard(path)
        allowed, remaining = guard.can_trade("BTC/USDT", 3600)
        assert allowed is True
        assert remaining is None


# ---------------------------------------------------------------------------
# position_cap_breach
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPositionCap:
    def test_under_cap_is_not_breach(self):
        assert position_cap_breach(150.0, 1000.0, 0.2) is False

    def test_over_cap_is_breach(self):
        assert position_cap_breach(250.0, 1000.0, 0.2) is True

    def test_zero_equity_fails_open(self):
        # No equity to size against → don't block; let the broker reject.
        assert position_cap_breach(100.0, 0.0, 0.2) is False

    def test_zero_cap_fails_open(self):
        assert position_cap_breach(100.0, 1000.0, 0.0) is False


# ---------------------------------------------------------------------------
# CryptoBroker.place_order (full flow with a fake exchange)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBrokerPlaceOrder:
    def _broker(self, tmp_path, exchange=None, **kwargs):
        return CryptoBroker(
            exchange=exchange or FakeExchange(),
            quote_budget=kwargs.get("quote_budget", 1000.0),
            max_position_fraction=kwargs.get("max_position_fraction", 0.2),
            cooldown_seconds=kwargs.get("cooldown_seconds", 14400.0),
            cooldown_state_path=tmp_path / "cooldowns.json",
        )

    def test_hold_rating_is_skipped(self, tmp_path):
        broker = self._broker(tmp_path)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.HOLD))
        assert result["status"] == "skipped"
        assert "Hold rating" in result["reason"]
        assert result["action"] == "HOLD"

    def test_buy_fills_when_balance_available(self, tmp_path):
        exchange = FakeExchange(price=50000.0, balances={"USDT": {"free": 5000.0, "total": 5000.0}})
        broker = self._broker(tmp_path, exchange=exchange, quote_budget=1000.0)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert result["status"] == "filled"
        assert result["action"] == "BUY"
        assert result["symbol"] == "BTC/USDT"
        assert result["price"] == 50000.0
        # 1000 USDT / 50000 = 0.02 BTC
        assert pytest.approx(result["amount"], rel=1e-6) == 0.02
        assert len(exchange.created_orders) == 1
        assert exchange.created_orders[0]["side"] == "buy"

    def test_buy_blocked_by_cooldown_after_first_order(self, tmp_path):
        exchange = FakeExchange(price=50000.0, balances={"USDT": {"free": 5000.0, "total": 5000.0}})
        broker = self._broker(tmp_path, exchange=exchange, cooldown_seconds=3600)
        first = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert first["status"] == "filled"
        second = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert second["status"] == "skipped"
        assert "cooldown" in second["reason"]
        assert len(exchange.created_orders) == 1

    def test_buy_skipped_when_no_quote_balance(self, tmp_path):
        exchange = FakeExchange(balances={"USDT": {"free": 0.0, "total": 0.0}})
        broker = self._broker(tmp_path, exchange=exchange)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert result["status"] == "skipped"
        assert "no free quote balance" in result["reason"]

    def test_buy_blocked_by_position_cap(self, tmp_path):
        # Existing BTC holdings worth more than the cap allows.
        # 0.5 BTC @ 50000 = 25000 existing; equity 25000 (USDT) → cap 20% = 5000.
        exchange = FakeExchange(
            price=50000.0,
            balances={
                "USDT": {"free": 5000.0, "total": 5000.0},
                "BTC": {"free": 0.5, "total": 0.5},
            },
        )
        broker = self._broker(
            tmp_path, exchange=exchange, quote_budget=1000.0, max_position_fraction=0.2
        )
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert result["status"] == "skipped"
        assert "position cap" in result["reason"]

    def test_sell_skipped_when_no_position(self, tmp_path):
        exchange = FakeExchange(balances={"USDT": {"free": 5000.0, "total": 5000.0}})
        broker = self._broker(tmp_path, exchange=exchange)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.SELL))
        assert result["status"] == "skipped"
        assert "no BTC position" in result["reason"]

    def test_sell_fills_existing_position(self, tmp_path):
        exchange = FakeExchange(
            price=60000.0,
            balances={
                "USDT": {"free": 0.0, "total": 0.0},
                "BTC": {"free": 0.1, "total": 0.1},
            },
        )
        broker = self._broker(tmp_path, exchange=exchange)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.SELL))
        assert result["status"] == "filled"
        assert result["action"] == "SELL"
        assert pytest.approx(result["amount"], rel=1e-6) == 0.1
        assert exchange.created_orders[0]["side"] == "sell"

    def test_api_error_surfaces_as_error_status(self, tmp_path):
        exchange = FakeExchange(raise_on_create=RuntimeError("exchange 500"))
        broker = self._broker(tmp_path, exchange=exchange)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.BUY))
        assert result["status"] == "error"
        assert "exchange 500" in result["reason"]

    def test_overweight_rating_also_buys(self, tmp_path):
        exchange = FakeExchange(price=50000.0, balances={"USDT": {"free": 5000.0, "total": 5000.0}})
        broker = self._broker(tmp_path, exchange=exchange, quote_budget=500.0)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.OVERWEIGHT))
        assert result["status"] == "filled"
        assert result["action"] == "BUY"

    def test_underweight_rating_also_sells(self, tmp_path):
        exchange = FakeExchange(
            price=60000.0,
            balances={"BTC": {"free": 0.2, "total": 0.2}},
        )
        broker = self._broker(tmp_path, exchange=exchange)
        result = broker.place_order("BTC-USD", _decision(PortfolioRating.UNDERWEIGHT))
        assert result["status"] == "filled"
        assert result["action"] == "SELL"
