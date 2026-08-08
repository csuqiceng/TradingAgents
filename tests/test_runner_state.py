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
from unittest.mock import MagicMock, patch

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
    def __init__(self, pos=None, stop_result=None, fail_position=False,
                 price_quote=None):
        self._pos = pos or {"symbol": "BTC/USDT", "base": "BTC", "total": 0.0,
                            "free": 0.0, "used": 0.0, "price": 0.0,
                            "value_quote": 0.0}
        self._stop_result = stop_result
        self._fail_position = fail_position
        self._price_quote = price_quote
        self.cooldown = SimpleNamespace(record=lambda *a, **k: None)

    def get_position(self, ticker):
        if self._fail_position:
            raise RuntimeError("exchange down")
        return dict(self._pos)

    def hard_stop_sell(self, ticker):
        return dict(self._stop_result)

    def _fetch_price(self, symbol):
        return self._price_quote


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


class TestGuardrailsQueries:
    def test_day_baseline_returns_first_non_null_equity_before(self):
        store = _make_store()
        store.start_cycle("BTC-USD", "2026-08-08", equity_before=None, price_at_decision=65000.0)
        store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=65500.0)
        assert store.get_day_baseline_equity() == 5000.0

    def test_day_baseline_skips_null_takes_next(self):
        store = _make_store()
        store.start_cycle("BTC-USD", "2026-08-08", equity_before=None, price_at_decision=65000.0)
        store.start_cycle("BTC-USD", "2026-08-08", equity_before=None, price_at_decision=65500.0)
        store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=66000.0)
        assert store.get_day_baseline_equity() == 5000.0

    def test_day_baseline_no_cycles_today_returns_none(self):
        store = _make_store()
        assert store.get_day_baseline_equity() is None

    def test_day_baseline_crosses_utc_midnight(self):
        import datetime as _dt
        store = _make_store()
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        start_of_day = _dt.datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=_dt.timezone.utc)
        yesterday_late = start_of_day - _dt.timedelta(minutes=1)   # 23:59 prev day UTC
        today_early = start_of_day + _dt.timedelta(minutes=1)      # 00:01 today UTC
        c_yesterday = store.start_cycle("BTC-USD", "2026-08-08",
                                        equity_before=9999.0, price_at_decision=65000.0)
        store._conn.execute("UPDATE cycles SET ts = ? WHERE id = ?",
                            (yesterday_late.timestamp(), c_yesterday))
        c_today = store.start_cycle("BTC-USD", "2026-08-08",
                                    equity_before=5000.0, price_at_decision=65500.0)
        store._conn.execute("UPDATE cycles SET ts = ? WHERE id = ?",
                            (today_early.timestamp(), c_today))
        store._conn.commit()
        assert store.get_day_baseline_equity() == 5000.0

    def test_peak_equity_returns_max_from_completed(self):
        store = _make_store()
        c1 = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=65000.0)
        store.complete_cycle(c1, status="completed", equity_after=5500.0)
        c2 = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5500.0, price_at_decision=65500.0)
        store.complete_cycle(c2, status="completed", equity_after=5000.0)
        assert store.get_peak_equity() == 5500.0

    def test_peak_equity_includes_error_cycles(self):
        store = _make_store()
        c1 = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=65000.0)
        store.complete_cycle(c1, status="completed", equity_after=5500.0)
        c2 = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5500.0, price_at_decision=65500.0)
        store.complete_cycle(c2, status="error", equity_after=6000.0, error="boom")
        assert store.get_peak_equity() == 6000.0

    def test_peak_equity_excludes_halted_cycles(self):
        store = _make_store()
        # Halted cycle carries a high, non-NULL equity_after so the test
        # genuinely proves STATUS-based exclusion (not just NULL filtering).
        c_halted = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=65000.0)
        store.complete_cycle(c_halted, status="halted", equity_after=9999.0)
        c_completed = store.start_cycle("BTC-USD", "2026-08-08", equity_before=5000.0, price_at_decision=65500.0)
        store.complete_cycle(c_completed, status="completed", equity_after=5500.0)
        assert store.get_peak_equity() == 5500.0

    def test_peak_equity_no_records_returns_none(self):
        store = _make_store()
        assert store.get_peak_equity() is None


def _guardrail_loop(store, daily_limit=-0.10, drawdown_limit=-0.15):
    """Build a TradingLoop wired only for guardrail checks (no broker/graph).

    _check_guardrails reads ``self.config`` (thresholds) and ``self.store``
    (baseline/peak queries); it never touches the broker or the graph, so we
    construct a real loop and override only the two attributes it needs.
    """
    loop = TradingLoop({
        "runner_daily_loss_limit": daily_limit,
        "runner_max_drawdown": drawdown_limit,
    })
    loop.store = store
    return loop


def _seed_baseline(store, equity_before=5000.0):
    """Insert one cycle today (UTC) so get_day_baseline_equity returns it."""
    store.start_cycle("BTC-USD", "2026-08-08", equity_before=equity_before,
                      price_at_decision=65000.0)


def _seed_peak(store, equity_after):
    """Insert one completed cycle today (UTC) with the given equity_after.

    equity_before is left None so the daily baseline stays None — callers that
    want a baseline too should pair this with _seed_baseline separately.
    """
    c = store.start_cycle("BTC-USD", "2026-08-08", equity_before=None,
                          price_at_decision=65000.0)
    store.complete_cycle(c, status="completed", equity_after=equity_after)


class TestCheckGuardrails:
    def test_daily_loss_triggers_halt(self):
        # baseline 5000, equity_before 4450 → -11% <= -10% → halt.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(4450.0) == (True, "daily_loss_limit")

    def test_daily_loss_below_threshold_passes(self):
        # baseline 5000, equity_before 4600 → -8% > -10% → OK.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(4600.0) == (False, "")

    def test_daily_loss_exact_threshold_halts(self):
        # baseline 5000, equity_before 4500 → exactly -10% → halt (<=).
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(4500.0) == (True, "daily_loss_limit")

    def test_drawdown_triggers_halt(self):
        # peak 6000, equity_before 5050 → -15.83% <= -15% → halt. baseline set
        # to 5000 so the daily check does not also fire (5050 vs 5000 = +1%).
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        _seed_peak(store, equity_after=6000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(5050.0) == (True, "max_drawdown")

    def test_drawdown_below_threshold_passes(self):
        # peak 6000, equity_before 5200 → -13.3% > -15% → OK.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        _seed_peak(store, equity_after=6000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(5200.0) == (False, "")

    def test_equity_before_none_passes(self):
        # snapshot failed → skip both checks (fail-open).
        store = _make_store()
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(None) == (False, "")

    def test_baseline_none_skips_daily_check(self):
        # baseline None (no non-NULL equity_before today), peak 6000,
        # equity_before 5200 → drawdown -13.3% > -15% → OK.
        store = _make_store()
        _seed_peak(store, equity_after=6000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(5200.0) == (False, "")

    def test_peak_none_skips_drawdown_check(self):
        # baseline 5000, no completed cycles (peak None), equity_before 4600
        # → daily -8% > -10% → OK.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(4600.0) == (False, "")

    def test_baseline_zero_skips_daily_check(self):
        # baseline 0 must be skipped (division-by-zero guard). equity_before 0.
        store = _make_store()
        _seed_baseline(store, equity_before=0.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(0.0) == (False, "")

    def test_peak_zero_skips_drawdown_check(self):
        # peak 0 must be skipped (division-by-zero guard). baseline None so the
        # daily check is also not exercised here.
        store = _make_store()
        _seed_peak(store, equity_after=0.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(5000.0) == (False, "")

    def test_both_trigger_composite_reason(self):
        # baseline 5000, peak 5900, equity_before 4450 → daily -11% (halt) and
        # drawdown (4450-5900)/5900 = -24.6% (halt) → composite reason.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        _seed_peak(store, equity_after=5900.0)
        loop = _guardrail_loop(store)
        assert loop._check_guardrails(4450.0) == (True, "daily_loss_limit+max_drawdown")

    def test_positive_threshold_raises_valueerror(self):
        # A positive daily-loss threshold is a misconfig → ValueError (not
        # swallowed by the fail-open try/except).
        store = _make_store()
        loop = _guardrail_loop(store, daily_limit=0.10)
        with pytest.raises(ValueError):
            loop._check_guardrails(5000.0)

    def test_state_query_exception_fail_open(self):
        # If get_day_baseline_equity raises, the guardrail must fail-open
        # (return OK) rather than crash the cycle. peak is None on an empty
        # store, so the drawdown check is also skipped.
        store = _make_store()
        loop = _guardrail_loop(store)

        def _raise():
            raise RuntimeError("db down")
        loop.store.get_day_baseline_equity = _raise
        assert loop._check_guardrails(5000.0) == (False, "")


def _run_once_loop(store, broker, *, daily_limit=-0.10, drawdown_limit=-0.15,
                   stop_loss_pct=10.0, equity_quote=4450.0):
    """Build a TradingLoop wired for run_once with guardrails.

    snapshot_account is stubbed to return a fixed equity so the halt decision
    is deterministic; get_broker returns ``broker`` (which must expose
    _fetch_price for the price_at_decision anchor). _check_stop_loss is left
    real — with a zero-position broker it returns None, exercising the real
    path without triggering. Callers that want a stop-loss use a positioned
    broker plus a seeded BUY order.
    """
    loop = TradingLoop({
        "runner_daily_loss_limit": daily_limit,
        "runner_max_drawdown": drawdown_limit,
        "crypto_stop_loss_pct": stop_loss_pct,
        "execution_enabled": True,
    })
    loop.store = store
    loop.snapshot_account = lambda: {"equity_quote": equity_quote}
    loop.get_broker = lambda: broker
    return loop


def _graph_must_not_run():
    """Stand-in for get_graph on halt-path tests: if run_once fails to
    short-circuit, calling get_graph raises instead of silently building the
    heavy graph."""
    raise AssertionError("get_graph/propagate must not run on a halted cycle")


class TestRunOnceGuardrailHalt:
    """Integration tests for the guardrail halt path inside run_once.

    These exercise the full run_once flow up to / through the halt branch:
    snapshot -> price anchor -> start_cycle -> stop-loss -> guardrail check ->
    halt early-return (or, for the non-halt case, the unchanged normal path).
    """

    def test_halt_skips_propagate(self):
        # baseline 5000, equity_before 4450 → -11% <= -10% → halt. The graph
        # must never be built / propagated.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)  # zero position → no stop-loss
        loop = _run_once_loop(store, broker, equity_quote=4450.0)

        propagate_calls = []

        def _propagate(*a, **k):
            propagate_calls.append(1)
            return ({}, "DECISION: HOLD BTC")

        graph = SimpleNamespace(
            propagate=_propagate,
            save_reports=lambda *a, **k: Path("/tmp/x"),
        )
        loop.get_graph = lambda: graph

        summary = loop.run_once("BTC-USD")

        assert summary["halted"] is True
        assert summary["halt_reason"] == "daily_loss_limit"
        assert summary["order_status"] == "halted"
        assert summary["equity_after"] is None
        assert propagate_calls == []

    def test_halt_stop_loss_still_runs(self):
        # Halt scenario, but a held position breaches the stop-loss first.
        # The stop-loss order is recorded on the cycle, AND the cycle still
        # ends with status='halted' — the two safety nets are independent.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        # Seed a filled BUY so _check_stop_loss has a VWAP entry to compare.
        c_buy = store.start_cycle("BTC-USD", "2026-08-08")
        store.record_order(c_buy, "BTC/USDT", "BUY", "filled", rating="Buy",
                           price=65000.0, amount=0.1)
        broker = FakeBroker(
            pos={"symbol": "BTC/USDT", "base": "BTC", "total": 0.1, "free": 0.1,
                 "used": 0.0, "price": 58000.0, "value_quote": 5800.0},
            stop_result={"status": "filled", "action": "SELL", "symbol": "BTC/USDT",
                         "price": 58000.0, "amount": 0.1, "reason": None,
                         "order": {"id": "sl1"}},
            price_quote=60000.0,
        )
        loop = _run_once_loop(store, broker, equity_quote=4450.0)
        # Guard against accidental propagate: halt must short-circuit.
        loop.get_graph = _graph_must_not_run

        summary = loop.run_once("BTC-USD")

        assert summary["halted"] is True
        assert summary["order_status"] == "halted"
        # Stop-loss SELL was recorded before the halt check fired.
        rows = store._conn.execute(
            "SELECT action, status FROM orders WHERE cycle_id = ?",
            (summary["cycle_id"],)).fetchall()
        assert any(r[0] == "SELL" and r[1] == "filled" for r in rows), rows
        # Cycle row is halted with NULL equity_after.
        row = store._conn.execute(
            "SELECT status, equity_after FROM cycles WHERE id = ?",
            (summary["cycle_id"],)).fetchone()
        assert row[0] == "halted"
        assert row[1] is None

    def test_halt_records_cycle_and_completes_halted(self):
        # Halt must still write a cycle row (audit trail) with status=halted
        # and equity_after NULL so it doesn't pollute the peak statistic.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)
        loop = _run_once_loop(store, broker, equity_quote=4450.0)
        loop.get_graph = _graph_must_not_run

        summary = loop.run_once("BTC-USD")

        row = store._conn.execute(
            "SELECT status, equity_after, order_status FROM cycles WHERE id = ?",
            (summary["cycle_id"],)).fetchone()
        assert row[0] == "halted"
        assert row[1] is None  # equity_after NULL
        assert row[2] == "halted"
        # No orders recorded on a halt cycle (no stop-loss, no LLM order).
        n_orders = store._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE cycle_id = ?",
            (summary["cycle_id"],)).fetchone()[0]
        assert n_orders == 0

    def test_non_halt_flow_unchanged(self):
        # equity_before 4600 vs baseline 5000 → -8% > -10% → no halt. The
        # normal path runs: propagate is called and the summary carries no
        # halted flag.
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)  # zero position → no stop-loss
        loop = _run_once_loop(store, broker, equity_quote=4600.0)

        propagate_calls = []

        def _propagate(*a, **k):
            propagate_calls.append(1)
            return ({"executed_order": {"status": "skipped", "action": "HOLD",
                                        "reason": "test"}},
                    "DECISION: HOLD BTC")

        graph = SimpleNamespace(
            propagate=_propagate,
            save_reports=lambda *a, **k: Path("/tmp/x"),
        )
        loop.get_graph = lambda: graph

        summary = loop.run_once("BTC-USD")

        assert "halted" not in summary or summary.get("halted") is False
        assert len(propagate_calls) == 1
        assert summary["order_status"] == "skipped"
        # Cycle completed normally (not halted).
        row = store._conn.execute(
            "SELECT status FROM cycles WHERE id = ?",
            (summary["cycle_id"],)).fetchone()
        assert row[0] == "completed"


class TestRunForeverGuardrailHalt:
    """Integration tests for halted-cycle handling in run_forever.

    A halted cycle ran no LLM and placed no orders, so it must not advance
    cycle_count — otherwise the reflection cadence would fire on a cycle that
    was deliberately skipped, burning LLM tokens for nothing. The cycle is
    still logged and audited; only the reflection/max_cycles accounting skips
    it.
    """

    @staticmethod
    def _halt_summary(cycle_id=1, ticker="BTC-USD"):
        return {
            "cycle_id": cycle_id,
            "ticker": ticker,
            "trade_date": "2026-08-08",
            "rating": None,
            "order_status": "halted",
            "equity_before": 4450.0,
            "equity_after": None,
            "report_path": None,
            "error": None,
            "halted": True,
            "halt_reason": "daily_loss_limit",
        }

    @staticmethod
    def _normal_summary(cycle_id=2, ticker="ETH-USD"):
        return {
            "cycle_id": cycle_id,
            "ticker": ticker,
            "trade_date": "2026-08-08",
            "rating": "Hold",
            "order_status": "skipped",
            "equity_before": 4450.0,
            "equity_after": 4450.0,
            "report_path": None,
            "error": None,
        }

    def test_halted_cycle_does_not_trigger_reflection(self):
        # Two tickers: the first halts (cycle_count stays 0), the second runs
        # normally and hits max_cycles=1 inside the for-loop, returning before
        # the post-loop reflection checks. With reflect_every=1 the reflection
        # would fire on a counted cycle — the halted cycle must not be counted.
        store = _make_store()
        loop = TradingLoop({
            "runner_tickers": "BTC-USD,ETH-USD",
            "runner_interval_seconds": 0.01,
            "runner_max_cycles": 1,
            "runner_reflect_every_n_cycles": 1,
            "runner_reflect_hold_every_n_cycles": 1,
        })
        loop.store = store

        loop.run_once = MagicMock(side_effect=[
            self._halt_summary(cycle_id=1, ticker="BTC-USD"),
            self._normal_summary(cycle_id=2, ticker="ETH-USD"),
        ])
        loop.reflect_on_trades = MagicMock()
        loop.reflect_on_decisions = MagicMock()

        with patch("tradingagents.runner.loop.time.sleep"):
            loop.run_forever()

        # Halted cycle didn't count, so run_once ran twice (halt + normal).
        assert loop.run_once.call_count == 2
        # Reflection never fired: max_cycles return happened inside the for-loop
        # before the post-loop reflection checks were reached.
        loop.reflect_on_trades.assert_not_called()
        loop.reflect_on_decisions.assert_not_called()

    def test_recovery_after_halt(self):
        # One ticker, two while-iterations: the first cycle halts (cycle_count
        # stays 0), the second runs normally and brings cycle_count to
        # max_cycles=1 → return. run_once must be called exactly twice,
        # proving the halted cycle was skipped rather than counted.
        store = _make_store()
        loop = TradingLoop({
            "runner_tickers": "BTC-USD",
            "runner_interval_seconds": 0.01,
            "runner_max_cycles": 1,
        })
        loop.store = store

        loop.run_once = MagicMock(side_effect=[
            self._halt_summary(cycle_id=1, ticker="BTC-USD"),
            self._normal_summary(cycle_id=2, ticker="BTC-USD"),
        ])
        loop.reflect_on_trades = MagicMock()
        loop.reflect_on_decisions = MagicMock()

        with patch("tradingagents.runner.loop.time.sleep"):
            loop.run_forever()

        assert loop.run_once.call_count == 2
