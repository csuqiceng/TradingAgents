"""Unit tests for the CLI execution status panel.

Rendering is pure: ``render_execution_panel`` takes a state dict and returns a
Rich Panel. We render to a string via a Console and assert on the text, so the
tests need no Live display / terminal. The ``ExecutionTracker`` state-machine
transitions are covered too.
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.panel import Panel

from cli.execution_panel import (
    _STATUS_STYLE,
    ExecutionTracker,
    render_execution_panel,
)


def _render_text(state: dict) -> str:
    """Render the panel to plain text for substring assertions."""
    panel = render_execution_panel(state)
    assert isinstance(panel, Panel)
    buf = io.StringIO()
    Console(file=buf, width=100, force_terminal=False, color_system=None).print(panel)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# render_execution_panel — pure rendering
# ---------------------------------------------------------------------------


class TestRenderExecutionPanel:
    def test_pending_state_shows_ticker_and_pending_status(self):
        text = _render_text({
            "enabled": True,
            "mode": "paper",
            "ticker": "BTC-USD",
            "asset_type": "crypto",
            "status": "pending",
        })
        assert "BTC-USD" in text
        assert "pending" in text
        assert "PAPER (testnet)" in text

    def test_filled_state_shows_price_and_amount(self):
        text = _render_text({
            "enabled": True,
            "mode": "live",
            "ticker": "BTC-USD",
            "rating": "Buy",
            "action": "BUY",
            "symbol": "BTC/USDT",
            "status": "filled",
            "price": 50000.0,
            "amount": 0.02,
        })
        assert "filled" in text
        assert "50000" in text
        assert "0.02" in text
        assert "Buy → BUY" in text
        assert "LIVE" in text

    def test_skipped_state_shows_reason_not_price(self):
        text = _render_text({
            "enabled": True,
            "mode": "paper",
            "ticker": "BTC-USD",
            "rating": "Hold",
            "action": "HOLD",
            "symbol": "BTC/USDT",
            "status": "skipped",
            "reason": "Hold rating on BTC-USD",
            "price": 0,  # must NOT appear for non-filled
            "amount": 0,
        })
        assert "skipped" in text
        assert "Hold rating on BTC-USD" in text
        # Price/amount rows are suppressed for non-filled statuses.
        assert "Price" not in text
        assert "Amount" not in text

    def test_error_state_shows_reason(self):
        text = _render_text({
            "enabled": True,
            "mode": "paper",
            "ticker": "BTC-USD",
            "status": "error",
            "reason": "exchange 500",
        })
        assert "error" in text
        assert "exchange 500" in text

    def test_live_mode_labeled_in_red(self):
        # We can't assert color in color_system=None mode, but the LIVE label
        # text is unconditional.
        text = _render_text({"enabled": True, "mode": "live", "status": "pending"})
        assert "LIVE" in text

    def test_paper_mode_labeled_as_testnet(self):
        text = _render_text({"enabled": True, "mode": "paper", "status": "pending"})
        assert "PAPER (testnet)" in text

    def test_status_style_table_is_complete(self):
        # Every status the panel can produce must have a style entry.
        for status in ("filled", "skipped", "error", "pending"):
            assert status in _STATUS_STYLE


# ---------------------------------------------------------------------------
# ExecutionTracker — state transitions
# ---------------------------------------------------------------------------


class TestExecutionTracker:
    def test_default_state_is_pending_and_disabled(self):
        tracker = ExecutionTracker()
        assert tracker.state["status"] == "pending"
        assert tracker.enabled is False

    def test_set_pending_populates_ticker(self):
        tracker = ExecutionTracker(enabled=True, mode="paper")
        tracker.set_pending("BTC-USD", "crypto")
        assert tracker.state["ticker"] == "BTC-USD"
        assert tracker.state["status"] == "pending"
        assert tracker.state["mode"] == "paper"

    def test_set_result_populates_filled_order(self):
        tracker = ExecutionTracker(enabled=True, mode="live")
        tracker.set_result(
            "BTC-USD",
            "crypto",
            {
                "status": "filled",
                "action": "BUY",
                "symbol": "BTC/USDT",
                "price": 50000.0,
                "amount": 0.02,
            },
            rating="Buy",
        )
        assert tracker.state["status"] == "filled"
        assert tracker.state["action"] == "BUY"
        assert tracker.state["price"] == 50000.0
        assert tracker.state["rating"] == "Buy"

    def test_set_result_populates_skipped_with_reason(self):
        tracker = ExecutionTracker(enabled=True, mode="paper")
        tracker.set_result(
            "BTC-USD",
            "crypto",
            {"status": "skipped", "reason": "cooldown active", "action": "BUY"},
            rating="Buy",
        )
        assert tracker.state["status"] == "skipped"
        assert tracker.state["reason"] == "cooldown active"

    def test_render_returns_panel_reflecting_current_state(self):
        tracker = ExecutionTracker(enabled=True, mode="paper")
        tracker.set_result(
            "BTC-USD",
            "crypto",
            {"status": "filled", "action": "BUY", "symbol": "BTC/USDT",
             "price": 60000.0, "amount": 0.5},
            rating="Buy",
        )
        panel = tracker.render()
        assert isinstance(panel, Panel)
        # Render and check the filled content surfaces.
        buf = io.StringIO()
        Console(file=buf, width=100, force_terminal=False, color_system=None).print(panel)
        text = buf.getvalue()
        assert "60000" in text
        assert "filled" in text
