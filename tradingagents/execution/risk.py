"""Lightweight risk guards that don't depend on a specific broker.

These run *before* the broker sees the order. They exist because the
framework's "risk management" agents are debate-style LLM roles, not hard
limits — a real account needs deterministic, code-level guards that cannot be
talked out of by a model.

Two guards ship here:

- ``CooldownGuard``: prevents the same symbol from being traded more often
  than a configured interval. The LLM decision layer is non-deterministic;
  without this, two near-identical runs minutes apart could double a position.
- ``position_cap_breach``: checks whether a proposed buy would push a single
  asset past the configured max-position fraction of account equity.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class CooldownGuard:
    """Per-symbol, per-direction cooldown backed by a JSON file on disk.

    Persistence is on disk (not in-memory) so the guard survives across
    separate process invocations — a typical deployment runs one
    ``propagate()`` per analysis, each in a fresh process. The file is a
    flat ``{"SYMBOL#direction": last_order_unix_ts}`` map.

    Cooldowns are tracked per *direction* (``buy`` / ``sell``) so a sell
    (e.g. a stop-loss exit) never blocks a later buy, and vice versa. This
    matters for exit timing: with a single shared cooldown, a fresh BUY would
    block a stop-loss SELL for hours and leave the position unprotected.

    Legacy state files written by the old single-direction guard (keys of the
    form ``SYMBOL`` without a ``#direction`` suffix) are read transparently:
    their timestamps age out naturally and are simply overwritten on the next
    ``record()``, so no migration step is needed.

    The guard is intentionally permissive on I/O failure: if the state file
    can't be read or written, the order is allowed through rather than
    blocking trading on a transient FS error. The broker itself is the last
    line of defense.
    """

    # Separator between symbol and direction in the state-file key. `#` cannot
    # appear in a ccxt symbol, so it is unambiguous.
    _DIR_SEP = "#"

    def __init__(self, state_path: str | os.PathLike[str] | None = None):
        if state_path is None:
            state_path = Path.home() / ".tradingagents" / "execution" / "cooldowns.json"
        self.state_path = Path(state_path)

    def _load(self) -> dict[str, float]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _dump(self, state: dict[str, float]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass  # see class docstring: fail open on FS errors.

    @staticmethod
    def _key(symbol: str, direction: str) -> str:
        return f"{symbol}{CooldownGuard._DIR_SEP}{direction.lower()}"

    def can_trade(
        self, symbol: str, cooldown_seconds: float, direction: str = "buy"
    ) -> tuple[bool, float | None]:
        """Return ``(allowed, remaining_seconds)`` for a trade in ``direction``.

        ``remaining_seconds`` is None when allowed, otherwise the wait time
        before the next order on ``symbol`` in that direction is permitted.
        """
        if cooldown_seconds <= 0:
            return True, None
        state = self._load()
        last = state.get(self._key(symbol, direction))
        if last is None:
            return True, None
        elapsed = time.time() - float(last)
        if elapsed >= cooldown_seconds:
            return True, None
        return False, cooldown_seconds - elapsed

    def record(self, symbol: str, direction: str = "buy") -> None:
        """Mark an order on ``symbol`` in ``direction`` as just placed (now)."""
        state = self._load()
        state[self._key(symbol, direction)] = time.time()
        self._dump(state)


def position_cap_breach(
    proposed_base_value: float,
    account_equity: float,
    max_position_fraction: float,
) -> bool:
    """Return True if buying ``proposed_base_value`` worth of an asset would
    push that asset's holdings past ``max_position_fraction`` of equity.

    ``proposed_base_value`` is the *post-trade* total value of the position
    (existing + new buy), in quote currency. The caller is responsible for
    adding existing holdings; this helper only does the ratio check so the
    rule lives in exactly one place.
    """
    if account_equity <= 0 or max_position_fraction <= 0:
        # No equity to size against, or cap disabled. Refuse to block on bad
        # inputs: treat as not-breached and let the broker's own balance
        # check reject the order if it's genuinely unaffordable.
        return False
    return (proposed_base_value / account_equity) > max_position_fraction
