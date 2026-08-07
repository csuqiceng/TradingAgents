"""Execution status panel for the TradingAgents CLI.

Renders a live view of the optional order-execution step that runs after the
Portfolio Manager produces its decision. The panel is only shown when
``execution_enabled`` is set in config; otherwise this module is never
imported and adds zero overhead to analysis-only runs.

The panel shows:
- the resolved rating and the action it mapped to,
- the order status (filled / skipped / error / pending),
- the symbol, price, amount, and a human-readable reason.

All rendering is pure: :func:`render_execution_panel` takes a state dict and
returns a Rich renderable, so it can be unit-tested without a Live display.
"""

from __future__ import annotations

from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Status → (color, symbol) for the status cell. Kept module-level so tests
# can assert against the same mapping the panel uses.
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "filled": ("green", "✓"),
    "skipped": ("yellow", "⊘"),
    "error": ("red", "✗"),
    "pending": ("cyan", "…"),
}


def _status_cell(status: str) -> Text:
    color, glyph = _STATUS_STYLE.get(status, ("white", "?"))
    return Text(f"{glyph} {status}", style=color)


def render_execution_panel(state: dict[str, Any]) -> Panel:
    """Render the execution status panel from a state dict.

    ``state`` keys (all optional except where noted):
        enabled:        bool  — whether execution is on at all
        ticker:         str   — instrument under analysis
        asset_type:     str   — "crypto" / "stock" / ...
        rating:         str   — PM rating ("Buy", "Hold", ...)
        action:         str   — mapped action ("BUY"/"SELL"/"HOLD")
        symbol:         str   — broker market symbol ("BTC/USDT")
        status:         str   — order status ("filled"/"skipped"/"error"/"pending")
        price:          float — fill/quote price
        amount:         float — base-asset quantity
        reason:         str   — explanation when not filled
        mode:           str   — "paper" | "live"

    A "pending" state (analysis still running, broker not yet invoked) is the
    initial view before the broker returns.
    """
    table = Table(show_header=True, header_style="bold magenta", box=None, padding=(0, 1))
    table.add_column("Field", style="cyan", width=14)
    table.add_column("Value", style="white", ratio=1)

    mode = state.get("mode", "paper")
    mode_label = "PAPER (testnet)" if mode == "paper" else "LIVE"
    mode_color = "yellow" if mode == "paper" else "bold red"

    status = state.get("status", "pending")
    rating = state.get("rating", "—")
    action = state.get("action", "—")

    table.add_row("Mode", Text(mode_label, style=mode_color))
    table.add_row("Ticker", state.get("ticker", "—"))
    table.add_row("Rating", f"{rating} → {action}" if action != "—" else rating)
    table.add_row("Symbol", state.get("symbol", "—"))
    table.add_row("Status", _status_cell(status))

    # Price/amount only meaningful when filled; show them only then so an
    # error/skip row doesn't display misleading zeros.
    if status == "filled":
        price = state.get("price")
        amount = state.get("amount")
        if price is not None:
            table.add_row("Price", f"{price}")
        if amount is not None:
            table.add_row("Amount", f"{amount}")

    reason = state.get("reason")
    if reason and status != "filled":
        table.add_row("Reason", Text(reason, style="dim"))

    title = "Order Execution"
    if not state.get("enabled", True):
        # Caller shouldn't render the panel when disabled, but defend anyway.
        title += " (disabled)"

    border = {
        "filled": "green",
        "error": "red",
        "skipped": "yellow",
        "pending": "cyan",
    }.get(status, "blue")

    return Panel(table, title=title, border_style=border, padding=(0, 1))


class ExecutionTracker:
    """Mutable state holder for the execution panel.

    The CLI's :class:`MessageBuffer` pattern (mutate state, then re-render)
    is mirrored here so the execution panel slots into the existing Live
    refresh loop without changing how the rest of the display updates.
    """

    def __init__(self, enabled: bool = False, mode: str = "paper"):
        self.enabled = enabled
        self.mode = mode
        self.state: dict[str, Any] = {
            "enabled": enabled,
            "mode": mode,
            "status": "pending",
        }

    def set_pending(self, ticker: str, asset_type: str, rating: str | None = None) -> None:
        """Mark execution as queued (broker not yet invoked)."""
        self.state = {
            "enabled": self.enabled,
            "mode": self.mode,
            "ticker": ticker,
            "asset_type": asset_type,
            "rating": rating,
            "status": "pending",
        }

    def set_result(self, ticker: str, asset_type: str, result: dict[str, Any], rating: str | None = None) -> None:
        """Populate state from a broker ``OrderResult`` dict."""
        self.state = {
            "enabled": self.enabled,
            "mode": self.mode,
            "ticker": ticker,
            "asset_type": asset_type,
            "rating": rating,
            "action": result.get("action"),
            "symbol": result.get("symbol"),
            "status": result.get("status", "error"),
            "price": result.get("price"),
            "amount": result.get("amount"),
            "reason": result.get("reason"),
        }

    def render(self) -> Panel:
        """Render the current state as a panel (pure)."""
        return render_execution_panel(self.state)
