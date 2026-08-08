"""Unit tests for the runner's state-store queries and hard stop-loss check.

These cover the loop/state layer that the broker-level tests deliberately
skip: hold-decision reflection input selection (the P1 misclassification
regression) and the code-level stop-loss boundary conditions.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.runner.loop import TradingLoop
from tradingagents.runner.state import RunnerStateStore


def _make_store() -> RunnerStateStore:
    return RunnerStateStore(Path(tempfile.mkdtemp()) / "runner.db")


def _backdate(store: RunnerStateStore, cycle_id: int, hours: float) -> None:
    """Age a cycle row so min_age_hours filters let it through."""
    conn = store._conn
    conn.execute("UPDATE cycles SET ts = ts - ? WHERE id = ?", (hours * 3600, cycle_id))
    conn.commit()


class TestHoldDecisionSelection:
    def test_only_genuine_hold_cycles_qualify(self):
        store = _make_store()
        # True HOLD: PM said Hold, nothing filled.
        c_hold = store.start_cycle("BTC-USD", "2026-08-08", price_at_decision=65000.0)
        store.record_order(c_hold, "BTC/USDT", "HOLD", "skipped", rating="Hold", reason="Hold rating")
        store.complete_cycle(c_hold, status="completed", rating="Hold", order_status="skipped",
                             decision_md="DECISION: HOLD BTC")
        # Wanted to BUY but blocked by cooldown: not a HOLD decision.
        c_blocked = store.start_cycle("ETH-USD", "2026-08-08", price_at_decision=3400.0)
        store.record_order(c_blocked, "ETH/USDT", "BUY", "skipped", rating="Buy", reason="cooldown active")
        store.complete_cycle(c_blocked, status="completed", rating="Buy", order_status="skipped",
                             decision_md="DECISION: BUY ETH")
        # Stop-loss cycle: a filled SELL happened — reviewed as a trade, not a HOLD.
        c_stop = store.start_cycle("SOL-USD", "2026-08-08", price_at_decision=150.0)
        store.record_order(c_stop, "SOL/USDT", "SELL", "filled", rating=None, price=135.0,
                           amount=6.0, reason="hard stop-loss")
        store.complete_cycle(c_stop, status="completed", rating="Hold", order_status="skipped",
                             decision_md="DECISION: HOLD SOL")
        _backdate(store, c_hold, 48)
        _backdate(store, c_blocked, 48)
        _backdate(store, c_stop, 48)

        holds = store.get_hold_decisions_for_reflection(min_age_hours=24, limit=10)
        tickers = [h["ticker"] for h in holds]
        assert tickers == ["BTC-USD"], f"expected only the genuine HOLD, got {tickers}"
        assert holds[0]["rating"] == "Hold"

    def test_already_reflected_hold_is_excluded(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08", price_at_decision=65000.0)
        store.record_order(c, "BTC/USDT", "HOLD", "skipped", rating="Hold", reason="Hold rating")
        store.complete_cycle(c, status="completed", rating="Hold", order_status="skipped",
                             decision_md="DECISION: HOLD")
        _backdate(store, c, 48)
        store.record_reflection(order_id=None, cycle_id=c, ticker="BTC-USD", action="HOLD",
                                rating="Hold", entry_price=65000.0, current_price=66000.0,
                                amount=None, pnl_quote=None, pnl_pct=1.5, holding_hours=48.0,
                                decision_md="DECISION: HOLD", reflection_text="ok", lessons="ok")
        assert store.get_hold_decisions_for_reflection(min_age_hours=24) == []

    def test_last_filled_buy_price(self):
        store = _make_store()
        c1 = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c1, "BTC/USDT", "BUY", "filled", rating="Buy", price=64000.0, amount=0.1)
        assert store.get_last_filled_buy_price("BTC/USDT") == 64000.0
        c2 = store.start_cycle("BTC-USD", "2026-08-09")
        store.record_order(c2, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.05)
        assert store.get_last_filled_buy_price("BTC/USDT") == 65000.0
        # Skipped orders never anchor the stop.
        c3 = store.start_cycle("BTC-USD", "2026-08-10")
        store.record_order(c3, "BTC/USDT", "BUY", "skipped", rating="Buy", reason="cooldown")
        assert store.get_last_filled_buy_price("BTC/USDT") == 65000.0


class FakeBroker:
    def __init__(self, pos=None, stop_result=None, fail_position=False):
        self._pos = pos or {"symbol": "BTC/USDT", "base": "BTC", "total": 0.0,
                            "free": 0.0, "price": 0.0}
        self._stop_result = stop_result
        self._fail_position = fail_position
        self.cooldown = SimpleNamespace(record=lambda *a, **k: None)

    def get_position(self, ticker):
        if self._fail_position:
            raise RuntimeError("exchange down")
        return dict(self._pos)

    def hard_stop_sell(self, ticker):
        return dict(self._stop_result)


def _loop_with(store, broker, stop_pct=10.0):
    loop = TradingLoop({
        "crypto_stop_loss_pct": stop_pct,
        "execution_enabled": True,
    })
    loop.store = store
    loop.get_broker = lambda: broker
    return loop


class TestStopLossCheck:
    def test_triggers_when_breached(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.1)
        broker = FakeBroker(
            pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.1, "free": 0.1, "price": 58000.0},
            stop_result={"status": "filled", "action": "SELL", "symbol": "BTC/USDT",
                         "price": 58000.0, "amount": 0.1, "reason": None, "order": {"id": "x"}},
        )
        res = _loop_with(store, broker)._check_stop_loss("BTC-USD")
        assert res is not None and res["status"] == "filled"

    def test_does_not_trigger_within_threshold(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.1)
        broker = FakeBroker(
            pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.1, "free": 0.1, "price": 63000.0},
        )
        assert _loop_with(store, broker)._check_stop_loss("BTC-USD") is None

    def test_skips_when_no_position(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.1)
        broker = FakeBroker(pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.0, "free": 0.0, "price": 58000.0})
        assert _loop_with(store, broker)._check_stop_loss("BTC-USD") is None

    def test_skips_without_tracked_entry_price(self):
        store = _make_store()  # no BUY orders recorded
        broker = FakeBroker(
            pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.1, "free": 0.1, "price": 50000.0},
        )
        assert _loop_with(store, broker)._check_stop_loss("BTC-USD") is None

    def test_disabled_when_stop_pct_zero(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.1)
        broker = FakeBroker(
            pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.1, "free": 0.1, "price": 10000.0},
        )
        loop = _loop_with(store, broker, stop_pct=0)
        assert loop._check_stop_loss("BTC-USD") is None

    def test_fails_open_on_position_fetch_error(self):
        store = _make_store()
        c = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c, "BTC/USDT", "BUY", "filled", rating="Buy", price=65000.0, amount=0.1)
        broker = FakeBroker(fail_position=True)
        assert _loop_with(store, broker)._check_stop_loss("BTC-USD") is None
