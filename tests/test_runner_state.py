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

    The halt-reflection LLM is stubbed to None (skip LLM prose) by default so
    halt-path tests don't try to build a real LLM client. Tests that exercise
    the reflection logic should override ``loop._get_reflection_llm``.
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
    # B1: stub the reflection LLM to None so halt-reflection records the event
    # without calling a real LLM. This also ensures _graph_must_not_run's
    # invariant (no graph construction on halt) is genuinely tested —
    # previously get_graph was called via _get_reflection_llm but the
    # AssertionError was silently swallowed.
    loop._get_reflection_llm = lambda: None
    return loop


def _graph_must_not_run():
    """Stand-in for get_graph on halt-path tests: if run_once fails to
    short-circuit (i.e. the halt branch doesn't return early), calling
    get_graph raises instead of silently building the heavy graph.

    Note: the halt-reflection LLM is stubbed separately via
    ``loop._get_reflection_llm = lambda: None`` in _run_once_loop. This guard
    catches the case where the halt branch falls through to the propagate()
    call below it — it does NOT guard against _reflect_on_halt (which now uses
    a lightweight LLM, not the full graph)."""
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


# ======================================================================
# B1: Halt-reflection tests (_reflect_on_halt + halt_events table)
# ======================================================================

class TestHaltEventsTable:
    """Tests for the halt_events table CRUD in RunnerStateStore."""

    def test_record_and_list_halt_event(self):
        store = _make_store()
        rowid = store.record_halt_event(
            cycle_id=42, ticker="BTC-USD", reason="daily_loss_limit",
            equity_before=4450.0,
            analysis_text="Strategy failed due to sudden drop.",
            lessons="Reduce position size during high volatility.",
        )
        assert isinstance(rowid, int)
        events = store.list_halt_events(limit=10)
        assert len(events) == 1
        e = events[0]
        assert e["cycle_id"] == 42
        assert e["ticker"] == "BTC-USD"
        assert e["reason"] == "daily_loss_limit"
        assert e["equity_before"] == 4450.0
        assert "Strategy failed" in e["analysis_text"]
        assert "Reduce position" in e["lessons"]

    def test_list_halt_events_orders_by_ts_desc(self):
        store = _make_store()
        id1 = store.record_halt_event(cycle_id=1, ticker="BTC-USD",
                                       reason="daily_loss_limit", equity_before=4000.0)
        time.sleep(0.01)
        id2 = store.record_halt_event(cycle_id=2, ticker="ETH-USD",
                                       reason="max_drawdown", equity_before=3000.0)
        events = store.list_halt_events(limit=10)
        assert events[0]["id"] == id2  # most recent first
        assert events[1]["id"] == id1

    def test_record_halt_event_with_null_fields(self):
        store = _make_store()
        store.record_halt_event(
            cycle_id=1, ticker=None, reason="max_drawdown",
            equity_before=None, analysis_text=None, lessons=None,
        )
        events = store.list_halt_events()
        assert len(events) == 1
        assert events[0]["ticker"] is None
        assert events[0]["equity_before"] is None
        assert events[0]["analysis_text"] is None
        assert events[0]["lessons"] is None


class TestReflectOnHalt:
    """Unit tests for _reflect_on_halt: LLM available, LLM unavailable, cooldown,
    lesson extraction, and memory log writing."""

    def _make_halt_loop(self, store, equity_before=4450.0):
        """Build a loop with a stubbed snapshot/broker for halt-reflection tests."""
        broker = FakeBroker(price_quote=60000.0)
        loop = TradingLoop({
            "runner_daily_loss_limit": -0.10,
            "runner_max_drawdown": -0.15,
            "crypto_stop_loss_pct": 10.0,
            "execution_enabled": True,
            "memory_log_path": str(Path(tempfile.mkdtemp()) / "memory.md"),
        })
        loop.store = store
        loop.snapshot_account = lambda: {"equity_quote": equity_before}
        loop.get_broker = lambda: broker
        return loop

    def test_halt_reflection_with_llm_writes_analysis_and_lessons(self):
        store = _make_store()
        loop = self._make_halt_loop(store)

        # Fake LLM that returns a multi-sentence analysis
        fake_msg = SimpleNamespace(content=(
            "The halt was triggered by a 11% daily loss exceeding the 10% limit. "
            "Recent BUY orders at high prices contributed to the drawdown as the "
            "market reversed sharply. This appears to be a market regime change "
            "rather than a strategy failure. The actionable rule is to reduce "
            "position size by 50% when daily volatility exceeds 5%."
        ))
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=fake_msg)
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(
            cycle_id=1, ticker="BTC-USD",
            halt_reason="daily_loss_limit", equity_before=4450.0,
        )

        events = store.list_halt_events()
        assert len(events) == 1
        e = events[0]
        assert e["reason"] == "daily_loss_limit"
        assert e["equity_before"] == 4450.0
        assert "11% daily loss" in e["analysis_text"]
        # Lesson should be the last sentence (with min length check)
        assert "reduce position size" in e["lessons"].lower()

    def test_halt_reflection_llm_unavailable_records_placeholder(self):
        store = _make_store()
        loop = self._make_halt_loop(store)
        loop._get_reflection_llm = lambda: None  # LLM unavailable

        loop._reflect_on_halt(
            cycle_id=1, ticker="BTC-USD",
            halt_reason="max_drawdown", equity_before=3000.0,
        )

        events = store.list_halt_events()
        assert len(events) == 1
        e = events[0]
        assert e["analysis_text"] == "(LLM unavailable — analysis skipped)"
        assert e["lessons"] is None

    def test_halt_reflection_llm_failure_records_error(self):
        store = _make_store()
        loop = self._make_halt_loop(store)
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(side_effect=RuntimeError("LLM timeout"))
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(
            cycle_id=1, ticker="BTC-USD",
            halt_reason="daily_loss_limit", equity_before=4450.0,
        )

        events = store.list_halt_events()
        assert len(events) == 1
        assert "LLM halt reflection failed" in events[0]["analysis_text"]
        assert events[0]["lessons"] is None

    def test_halt_reflection_cooldown_skips_llm(self):
        store = _make_store()
        loop = self._make_halt_loop(store)
        loop.config["runner_halt_reflection_cooldown_seconds"] = 3600

        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=SimpleNamespace(content="Analysis."))
        loop._get_reflection_llm = lambda: fake_llm

        # First halt: runs LLM
        loop._reflect_on_halt(cycle_id=1, ticker="BTC-USD",
                              halt_reason="daily_loss_limit", equity_before=4450.0)
        assert fake_llm.invoke.call_count == 1

        # Second halt within cooldown: skips LLM
        loop._reflect_on_halt(cycle_id=2, ticker="BTC-USD",
                              halt_reason="daily_loss_limit", equity_before=4400.0)
        assert fake_llm.invoke.call_count == 1  # still 1, not 2

        # Second halt event still recorded (with cooldown note)
        events = store.list_halt_events()
        assert len(events) == 2
        assert "cooldown" in events[0]["analysis_text"].lower()

    def test_halt_reflection_lesson_extraction_handles_decimals(self):
        """I1 regression: split('.') would mangle '1.5x' → '1' + '5x'.
        The re.split approach should preserve it."""
        store = _make_store()
        loop = self._make_halt_loop(store)

        fake_msg = SimpleNamespace(content=(
            "The drawdown was caused by overleveraging at 1.5x. "
            "The market dropped 12% in one hour. "
            "Reduce leverage to 0.8x during high volatility periods."
        ))
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=fake_msg)
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(cycle_id=1, ticker="BTC-USD",
                              halt_reason="daily_loss_limit+max_drawdown",
                              equity_before=4450.0)

        events = store.list_halt_events()
        lesson = events[0]["lessons"]
        # Lesson should NOT be a mangled fragment like "8x."
        assert len(lesson) >= 20
        assert "0.8x" in lesson or "leverage" in lesson.lower()

    def test_halt_reflection_no_recent_trades(self):
        """I6: when no filled trades exist, prompt should say '(no recent filled trades)'."""
        store = _make_store()
        loop = self._make_halt_loop(store)

        captured_prompt = []
        fake_llm = MagicMock()
        def _capture_invoke(msgs):
            captured_prompt.append(msgs[1].content)  # HumanMessage
            return SimpleNamespace(content="Analysis text here is long enough.")
        fake_llm.invoke = _capture_invoke
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(cycle_id=1, ticker="BTC-USD",
                              halt_reason="daily_loss_limit", equity_before=4450.0)

        assert len(captured_prompt) == 1
        assert "(no recent filled trades)" in captured_prompt[0]

    def test_halt_reflection_composite_reason_humanized(self):
        """S2: composite reason 'daily_loss_limit+max_drawdown' should be
        humanized to 'daily loss limit + max drawdown' in the prompt."""
        store = _make_store()
        loop = self._make_halt_loop(store)

        captured_prompt = []
        fake_llm = MagicMock()
        def _capture_invoke(msgs):
            captured_prompt.append(msgs[1].content)
            return SimpleNamespace(content="Analysis text here is long enough.")
        fake_llm.invoke = _capture_invoke
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(cycle_id=1, ticker="BTC-USD",
                              halt_reason="daily_loss_limit+max_drawdown",
                              equity_before=4450.0)

        assert "daily loss limit + max drawdown" in captured_prompt[0]

    def test_halt_reflection_failure_does_not_arm_cooldown(self):
        """E3 regression: if LLM invoke fails, the cooldown timestamp must
        NOT be updated — otherwise a transient failure during a sustained
        drawdown would silence all subsequent halt reflections for the
        entire cooldown window."""
        store = _make_store()
        loop = self._make_halt_loop(store)
        loop.config["runner_halt_reflection_cooldown_seconds"] = 3600

        # First halt: LLM raises → lessons=None → cooldown NOT armed
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(side_effect=RuntimeError("network error"))
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(cycle_id=1, ticker="BTC-USD",
                              halt_reason="daily_loss_limit", equity_before=4450.0)
        assert fake_llm.invoke.call_count == 1
        assert loop._last_halt_reflection_ts == 0.0  # NOT armed

        # Second halt immediately after: LLM should be called again (no cooldown)
        fake_llm.invoke = MagicMock(return_value=SimpleNamespace(
            content="The halt was caused by a sharp drop. "
                    "Reduce position size during high volatility periods."
        ))
        loop._get_reflection_llm = lambda: fake_llm

        loop._reflect_on_halt(cycle_id=2, ticker="BTC-USD",
                              halt_reason="daily_loss_limit", equity_before=4400.0)
        assert fake_llm.invoke.call_count == 1  # called once for the second halt


class TestRunOnceHaltReflectionIntegration:
    """Integration: run_once halt path triggers _reflect_on_halt and records
    in halt_events table."""

    def test_halt_writes_halt_event_to_db(self):
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)
        loop = _run_once_loop(store, broker, equity_quote=4450.0)

        # Stub LLM to None — halt event should still be recorded
        loop._get_reflection_llm = lambda: None
        loop.get_graph = _graph_must_not_run

        summary = loop.run_once("BTC-USD")

        assert summary["halted"] is True
        events = store.list_halt_events()
        assert len(events) == 1
        assert events[0]["cycle_id"] == summary["cycle_id"]
        assert events[0]["reason"] == "daily_loss_limit"
        assert events[0]["equity_before"] == 4450.0

    def test_halt_reflection_failure_does_not_block_halt(self):
        """If _reflect_on_halt raises, the halt must still complete_cycle."""
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)
        loop = _run_once_loop(store, broker, equity_quote=4450.0)

        # Make _reflect_on_halt raise — halt must still complete
        loop._reflect_on_halt = MagicMock(side_effect=RuntimeError("reflection crashed"))
        loop.get_graph = _graph_must_not_run

        summary = loop.run_once("BTC-USD")

        assert summary["halted"] is True
        assert summary["order_status"] == "halted"
        # Cycle still completed with halted status
        row = store._conn.execute(
            "SELECT status FROM cycles WHERE id = ?",
            (summary["cycle_id"],)).fetchone()
        assert row[0] == "halted"

    def test_halt_with_llm_writes_lesson_to_memory_log(self):
        """End-to-end: halt + fake LLM → lesson written to memory log file."""
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)
        mem_path = Path(tempfile.mkdtemp()) / "trading_memory.md"
        loop = TradingLoop({
            "runner_daily_loss_limit": -0.10,
            "runner_max_drawdown": -0.15,
            "crypto_stop_loss_pct": 10.0,
            "execution_enabled": True,
            "memory_log_path": str(mem_path),
        })
        loop.store = store
        loop.snapshot_account = lambda: {"equity_quote": 4450.0}
        loop.get_broker = lambda: broker
        loop.get_graph = _graph_must_not_run

        fake_msg = SimpleNamespace(content=(
            "The halt was caused by a sharp 11% daily drop. "
            "Recent trades did not contribute significantly. "
            "This is normal BTC volatility, not a strategy failure. "
            "Reduce position size when daily ATR exceeds 5% of equity."
        ))
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=fake_msg)
        loop._get_reflection_llm = lambda: fake_llm

        summary = loop.run_once("BTC-USD")

        assert summary["halted"] is True
        # Memory log should contain the HALT-LESSON entry
        assert mem_path.exists()
        content = mem_path.read_text(encoding="utf-8")
        assert "HALT-LESSON" in content
        assert "Reduce position size" in content
        assert "<!-- ENTRY_END -->" in content

    def test_halt_lesson_memory_log_round_trips_through_parser(self):
        """E6: verify the HALT-LESSON entry written by halt-reflection can be
        parsed back by TradingMemoryLog.load_entries() / get_past_context(),
        so future PM runs actually see the lesson."""
        store = _make_store()
        _seed_baseline(store, equity_before=5000.0)
        broker = FakeBroker(price_quote=60000.0)
        mem_path = Path(tempfile.mkdtemp()) / "trading_memory.md"
        loop = TradingLoop({
            "runner_daily_loss_limit": -0.10,
            "runner_max_drawdown": -0.15,
            "crypto_stop_loss_pct": 10.0,
            "execution_enabled": True,
            "memory_log_path": str(mem_path),
        })
        loop.store = store
        loop.snapshot_account = lambda: {"equity_quote": 4450.0}
        loop.get_broker = lambda: broker
        loop.get_graph = _graph_must_not_run

        fake_msg = SimpleNamespace(content=(
            "The halt was caused by a sharp 11% daily drop. "
            "Recent trades did not contribute significantly. "
            "This is normal BTC volatility, not a strategy failure. "
            "Reduce position size when daily ATR exceeds 5% of equity."
        ))
        fake_llm = MagicMock()
        fake_llm.invoke = MagicMock(return_value=fake_msg)
        loop._get_reflection_llm = lambda: fake_llm

        loop.run_once("BTC-USD")

        # Now parse it back through the actual memory log parser
        from tradingagents.agents.utils.memory import TradingMemoryLog
        mem_log = TradingMemoryLog({"memory_log_path": str(mem_path)})
        entries = mem_log.load_entries()
        assert len(entries) >= 1
        halt_entry = [e for e in entries if e.get("rating") == "HALT-LESSON"]
        assert len(halt_entry) == 1, f"expected 1 HALT-LESSON entry, got {halt_entry}"
        e = halt_entry[0]
        assert e["ticker"] == "BTC-USD"
        assert e["pending"] is False
        assert "Reduce position size" in e["reflection"]
        assert "Halt reflection" in e["decision"]

        # S3: verify get_past_context() includes the HALT-LESSON entry so
        # future PM runs actually see the lesson in their context window.
        context = mem_log.get_past_context("BTC-USD")
        assert "HALT-LESSON" in context
        assert "Reduce position size" in context
