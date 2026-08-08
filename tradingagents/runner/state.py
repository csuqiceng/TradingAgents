"""SQLite-backed local state store for the autonomous runner.

Three tables, one file (``runner_state.db`` under the data cache dir):

- ``positions``  — latest per-symbol holding snapshot (upserted each cycle)
- ``orders``     — append-only order history (one row per filled/skipped/error)
- ``cycles``     — append-only run log (one row per loop iteration)

The store is the runner's source of truth for "what do I hold right now?".
It is reconciled against the exchange via ``CryptoBroker.get_account_snapshot``
at the start of each cycle, so a drifted local row never causes a bad decision
— the exchange is always authoritative, the DB is a fast cache + audit log.

Concurrency: the runner is single-process (one loop, sequential cycles), so we
use a plain ``sqlite3`` connection without extra locking. If a future refactor
adds concurrent cycles, switch to WAL + a lock.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT PRIMARY KEY,        -- ccxt symbol e.g. BTC/USDT
    base          TEXT NOT NULL,           -- e.g. BTC
    quote         TEXT NOT NULL,           -- e.g. USDT
    free          REAL NOT NULL DEFAULT 0,
    used          REAL NOT NULL DEFAULT 0,
    total         REAL NOT NULL DEFAULT 0,
    price         REAL,                    -- last price used for valuation
    value_quote   REAL,                    -- total * price
    source        TEXT NOT NULL,           -- 'exchange' | 'order'
    updated_at    REAL NOT NULL            -- unix seconds
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id      INTEGER,                 -- FK-ish to cycles.rowid
    ts            REAL NOT NULL,           -- when the order result was recorded
    symbol        TEXT,
    action        TEXT,                    -- BUY | SELL | HOLD
    status        TEXT,                    -- filled | skipped | error
    rating        TEXT,                    -- PM rating that triggered it
    price         REAL,
    amount        REAL,
    reason        TEXT,                    -- skip/error reason
    exchange_order_id TEXT,                -- broker order id on fill
    raw           TEXT                     -- JSON of the full OrderResult
);

CREATE TABLE IF NOT EXISTS cycles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,           -- cycle start time
    ticker        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    status        TEXT NOT NULL,           -- running | completed | error
    rating        TEXT,                    -- PM rating (filled when completed)
    order_status  TEXT,                    -- filled | skipped | error | none
    equity_before REAL,
    equity_after  REAL,
    duration_s    REAL,
    error         TEXT,                    -- traceback when status=error
    started_at    REAL,
    ended_at      REAL
);

-- Trade-level reflection log. One row per reflected trade (a filled order
-- that has been reviewed post-hoc with PnL + LLM-generated lessons). This is
-- the "what did I learn from actually trading" layer, complementing the
-- existing price-only reflection in TradingMemoryLog.
CREATE TABLE IF NOT EXISTS trade_reflections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,         -- when the reflection was generated
    order_id        INTEGER,               -- FK to orders.id
    cycle_id        INTEGER,               -- FK to cycles.id (the decision cycle)
    ticker          TEXT NOT NULL,
    action          TEXT,                  -- BUY | SELL (HOLD never trades)
    rating          TEXT,                  -- PM rating that drove the trade
    entry_price     REAL,                  -- fill price from the order
    current_price   REAL,                  -- price at reflection time
    amount          REAL,                  -- base-asset qty traded
    pnl_quote       REAL,                  -- realized+unrealized PnL in quote
    pnl_pct         REAL,                  -- PnL as % of entry cost
    holding_hours   REAL,                  -- hours between order and reflection
    decision_md     TEXT,                  -- the PM decision text that drove it
    reflection_text TEXT,                  -- LLM-generated reflection (2-4 sentences)
    lessons         TEXT,                  -- concrete lessons for future runs
    reflected       INTEGER NOT NULL DEFAULT 1  -- 1=done, 0=pending
);

CREATE INDEX IF NOT EXISTS idx_orders_cycle ON orders(cycle_id);
CREATE INDEX IF NOT EXISTS idx_orders_ts    ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_cycles_ts    ON cycles(ts);
CREATE INDEX IF NOT EXISTS idx_reflections_order ON trade_reflections(order_id);
CREATE INDEX IF NOT EXISTS idx_reflections_ts    ON trade_reflections(ts);

CREATE TABLE IF NOT EXISTS halt_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    cycle_id        INTEGER NOT NULL,
    ticker          TEXT,
    reason          TEXT NOT NULL,
    equity_before   REAL,
    analysis_text   TEXT,
    lessons         TEXT
);

CREATE INDEX IF NOT EXISTS idx_halt_events_ts ON halt_events(ts);
"""

# --- Idempotent schema migrations for pre-existing DBs ---------------------
# Older runner_state.db files (created before trade_reflections existed) need
# the new table + the cycles.decision_md column added. All migrations are
# idempotent: they check before altering so re-running on a fresh DB is a no-op.
_MIGRATIONS = [
    # Add decision_md to cycles (stores the full PM decision markdown so
    # reflection can review "what did I think" alongside "what happened").
    "ALTER TABLE cycles ADD COLUMN decision_md TEXT",
    # Add price_at_decision to cycles: the live spot price at cycle start,
    # used by hold-decision reflection to evaluate "did the market move
    # against/for my HOLD since I decided?".
    "ALTER TABLE cycles ADD COLUMN price_at_decision REAL",
    # Add report_path to cycles: filesystem location of the full analysis
    # report tree (analysts / debate / trader / risk / PM) saved each cycle.
    "ALTER TABLE cycles ADD COLUMN report_path TEXT",
]


class RunnerStateStore:
    """SQLite store for runner positions, orders, and cycle history."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the loop may be constructed on one thread
        # and run on another (e.g. under schedule). sqlite3 connections are
        # fine for cross-thread use as long as we don't share cursors.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        # Idempotent migrations for pre-existing DBs: ALTER TABLE ADD COLUMN
        # fails if the column already exists, so we catch and ignore that
        # specific error. (PRAGMA table_info would also work but is more
        # verbose for a single column.)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Positions
    # ------------------------------------------------------------------ #

    def upsert_position(
        self,
        symbol: str,
        base: str,
        quote: str,
        free: float,
        used: float,
        total: float,
        price: float | None,
        value_quote: float | None,
        source: str = "exchange",
    ) -> None:
        """Insert or replace the position row for ``symbol``."""
        self._conn.execute(
            """
            INSERT INTO positions
                (symbol, base, quote, free, used, total, price, value_quote,
                 source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                base=excluded.base, quote=excluded.quote, free=excluded.free,
                used=excluded.used, total=excluded.total, price=excluded.price,
                value_quote=excluded.value_quote, source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (symbol, base, quote, free, used, total, price, value_quote,
             source, time.time()),
        )
        self._conn.commit()

    def upsert_position_from_snapshot(self, base: str, quote: str, row: dict[str, Any], price: float | None) -> None:
        """Upsert a single asset row from ``CryptoBroker.get_account_snapshot``.

        ``row`` is the per-asset dict {free, used, total}. ``price`` is the
        current price of ``base/quote`` (None when not a trading pair we track).
        """
        symbol = f"{base}/{quote}"
        total = float(row.get("total") or 0)
        value_quote = total * price if price else None
        self.upsert_position(
            symbol=symbol, base=base, quote=quote,
            free=float(row.get("free") or 0),
            used=float(row.get("used") or 0),
            total=total,
            price=price,
            value_quote=value_quote,
            source="exchange",
        )

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE symbol = ?", (symbol,)
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def list_positions(self) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM positions WHERE total > 0 ORDER BY value_quote DESC"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def record_order(
        self,
        cycle_id: int | None,
        symbol: str | None,
        action: str,
        status: str,
        rating: str | None = None,
        price: float | None = None,
        amount: float | None = None,
        reason: str | None = None,
        exchange_order_id: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> int:
        """Append an order row. Returns the inserted rowid."""
        cur = self._conn.execute(
            """
            INSERT INTO orders
                (cycle_id, ts, symbol, action, status, rating, price, amount,
                 reason, exchange_order_id, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cycle_id, time.time(), symbol, action, status, rating, price,
             amount, reason, exchange_order_id,
             json.dumps(raw, default=str) if raw else None),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Cycles
    # ------------------------------------------------------------------ #

    def start_cycle(
        self,
        ticker: str,
        trade_date: str,
        equity_before: float | None = None,
        price_at_decision: float | None = None,
    ) -> int:
        """Insert a cycle row in 'running' state. Returns its rowid.

        ``price_at_decision`` is the live spot price when the cycle started;
        it anchors hold-decision reflection (price move since the decision).
        """
        cur = self._conn.execute(
            """
            INSERT INTO cycles (ts, ticker, trade_date, status, equity_before,
                                started_at, price_at_decision)
            VALUES (?, ?, ?, 'running', ?, ?, ?)
            """,
            (time.time(), ticker, trade_date, equity_before, time.time(),
             price_at_decision),
        )
        self._conn.commit()
        return cur.lastrowid

    def complete_cycle(
        self,
        cycle_id: int,
        status: str = "completed",
        rating: str | None = None,
        order_status: str | None = None,
        equity_after: float | None = None,
        error: str | None = None,
        decision_md: str | None = None,
        report_path: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE cycles SET
                status = ?, rating = ?, order_status = ?, equity_after = ?,
                error = ?, ended_at = ?, duration_s = ? - started_at,
                decision_md = ?, report_path = ?
            WHERE id = ?
            """,
            (status, rating, order_status, equity_after, error, time.time(),
             time.time(), decision_md, report_path, cycle_id),
        )
        self._conn.commit()

    def list_cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM cycles ORDER BY ts DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def last_cycle(self) -> dict[str, Any] | None:
        rows = self.list_cycles(limit=1)
        return rows[0] if rows else None

    def count_cycles(self, ticker: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM cycles WHERE ticker = ?", (ticker,)
        )
        return cur.fetchone()[0]

    # ------------------------------------------------------------------ #
    # Trade reflections
    # ------------------------------------------------------------------ #

    def record_reflection(
        self,
        order_id: int | None,
        cycle_id: int | None,
        ticker: str,
        action: str,
        rating: str | None,
        entry_price: float | None,
        current_price: float | None,
        amount: float | None,
        pnl_quote: float | None,
        pnl_pct: float | None,
        holding_hours: float | None,
        decision_md: str | None,
        reflection_text: str,
        lessons: str | None = None,
    ) -> int:
        """Insert a trade reflection row. Returns the inserted rowid."""
        cur = self._conn.execute(
            """
            INSERT INTO trade_reflections
                (ts, order_id, cycle_id, ticker, action, rating, entry_price,
                 current_price, amount, pnl_quote, pnl_pct, holding_hours,
                 decision_md, reflection_text, lessons, reflected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (time.time(), order_id, cycle_id, ticker, action, rating,
             entry_price, current_price, amount, pnl_quote, pnl_pct,
             holding_hours, decision_md, reflection_text, lessons),
        )
        self._conn.commit()
        return cur.lastrowid

    def record_halt_event(
        self,
        cycle_id: int,
        ticker: str | None,
        reason: str,
        equity_before: float | None,
        analysis_text: str | None = None,
        lessons: str | None = None,
    ) -> int:
        """Record a guardrail halt event. Returns the inserted rowid."""
        cur = self._conn.execute(
            """
            INSERT INTO halt_events
                (ts, cycle_id, ticker, reason, equity_before, analysis_text, lessons)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), cycle_id, ticker, reason, equity_before, analysis_text, lessons),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_halt_events(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM halt_events ORDER BY ts DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def list_reflections(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM trade_reflections ORDER BY ts DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def reflected_order_ids(self) -> set[int]:
        """Return the set of order ids that already have a reflection."""
        cur = self._conn.execute(
            "SELECT order_id FROM trade_reflections WHERE order_id IS NOT NULL"
        )
        return {row[0] for row in cur.fetchall()}

    def get_filled_orders_for_reflection(self, min_age_hours: float = 0) -> list[dict[str, Any]]:
        """Return filled orders that are older than ``min_age_hours`` and not
        yet reflected, joined with their cycle's decision_md.

        This is the input set for ``TradingLoop.reflect_on_trades()``.
        """
        cutoff = time.time() - min_age_hours * 3600
        cur = self._conn.execute(
            """
            SELECT o.id AS order_id, o.cycle_id, o.ts AS order_ts,
                   o.symbol, o.action, o.rating, o.price AS entry_price,
                   o.amount, o.exchange_order_id,
                   c.ticker, c.trade_date, c.decision_md
            FROM orders o
            LEFT JOIN cycles c ON o.cycle_id = c.id
            WHERE o.status = 'filled'
              AND o.ts <= ?
              AND o.id NOT IN (SELECT order_id FROM trade_reflections
                               WHERE order_id IS NOT NULL)
            ORDER BY o.ts ASC
            """,
            (cutoff,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_last_filled_buy_price(self, symbol: str) -> float | None:
        """Return the fill price of the most recent filled BUY on ``symbol``.

        NOTE: this is the *last* buy price, NOT the position cost. With
        multiple fills it diverges from the true blended entry (e.g. buying
        0.1 @ 60000 then 0.05 @ 70000 returns 70000, not the 63333 VWAP).
        New stop-loss anchoring should use :meth:`get_vwap_entry_price`
        instead. Retained for callers that specifically want the most recent
        fill.

        Returns None when there is no filled buy on record (e.g. a position
        opened manually outside the runner).
        """
        cur = self._conn.execute(
            """
            SELECT price FROM orders
            WHERE symbol = ? AND action = 'BUY' AND status = 'filled'
              AND price IS NOT NULL
            ORDER BY ts DESC LIMIT 1
            """,
            (symbol,),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None

    def get_vwap_entry_price(self, symbol: str) -> float | None:
        """Return the volume-weighted average price (VWAP) of all filled BUY
        orders on ``symbol``.

        This is the correct entry-price anchor for the hard stop-loss: when
        the runner has added to a position across multiple fills (e.g.
        0.1 @ 60000 then 0.05 @ 70000), the VWAP (63333) reflects the true
        blended cost, whereas the last-fill price (70000) would stop out too
        early.

        Returns None when there is no filled BUY on record (e.g. a position
        opened manually outside the runner) — the stop-loss then stays
        dormant rather than guessing an entry.
        """
        cur = self._conn.execute(
            """
            SELECT price, amount FROM orders
            WHERE symbol = ? AND action = 'BUY' AND status = 'filled'
              AND price IS NOT NULL AND amount IS NOT NULL AND amount > 0
            ORDER BY ts ASC
            """,
            (symbol,),
        )
        total_cost = 0.0
        total_qty = 0.0
        for price, amount in cur.fetchall():
            total_cost += price * amount
            total_qty += amount
        if total_qty <= 0:
            return None
        return total_cost / total_qty

    def get_hold_decisions_for_reflection(
        self, min_age_hours: float = 0, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return completed HOLD cycles not yet reflected on.

        This is the input set for ``TradingLoop.reflect_on_decisions()`` —
        the "did my no-trade call age well?" review.

        Only *genuine* HOLD decisions qualify, so the review never fabricates
        a "no-trade" narrative for a cycle that actually wanted to trade:
          - ``c.rating = 'Hold'`` excludes cycles whose PM said BUY/SELL but
            got blocked by cooldown / position cap (those are "wanted to
            trade, couldn't", not "decided not to trade").
          - ``NOT EXISTS filled order`` excludes cycles where a hard
            stop-loss actually sold (those are already reviewed as real
            trades by ``reflect_on_trades`` — double-reviewing them as HOLD
            would write contradictory lessons).
        A cycle counts as unreflected when no trade_reflections row references
        it with action='HOLD'. Joined with the live anchor price captured at
        cycle start (``price_at_decision``).
        """
        cutoff = time.time() - min_age_hours * 3600
        cur = self._conn.execute(
            """
            SELECT c.id AS cycle_id, c.ts AS cycle_ts, c.ticker, c.trade_date,
                   c.decision_md, c.price_at_decision, c.rating
            FROM cycles c
            WHERE c.status = 'completed'
              AND c.rating = 'Hold'
              AND c.decision_md IS NOT NULL
              AND c.ts <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM orders o
                  WHERE o.cycle_id = c.id AND o.status = 'filled'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM trade_reflections r
                  WHERE r.cycle_id = c.id AND r.action = 'HOLD'
              )
            ORDER BY c.ts ASC
            LIMIT ?
            """,
            (cutoff, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_day_baseline_equity(self) -> float | None:
        """Return today's first non-NULL ``equity_before`` (UTC natural-day window).

        Anchors the daily P&L guardrail: "how much did I start the day with?"
        so intra-day drawdown is measured against the day's opening equity,
        not the all-time peak. The window is [00:00, 24:00) UTC so the
        baseline resets cleanly at UTC midnight regardless of the machine's
        local timezone.

        Returns None when no cycle has a non-NULL ``equity_before`` yet today.
        """
        import datetime as _dt
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        start_of_day = _dt.datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=_dt.timezone.utc)
        end_of_day = start_of_day + _dt.timedelta(days=1)
        ts_start = start_of_day.timestamp()
        ts_end = end_of_day.timestamp()
        cur = self._conn.execute(
            """
            SELECT equity_before FROM cycles
            WHERE ts >= ? AND ts < ? AND equity_before IS NOT NULL
            ORDER BY id ASC LIMIT 1
            """,
            (ts_start, ts_end),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None

    def get_peak_equity(self) -> float | None:
        """Return the max ``equity_after`` across all completed/error cycles.

        Anchors the all-time drawdown guardrail (highest equity watermark
        seen since the runner started trading). Only cycles that actually
        finished (``completed`` or ``error``) count — ``running`` cycles are
        provisional and ``halted`` cycles are excluded so an abandoned
        mid-cycle snapshot can't anchor the watermark.

        Returns None when no qualifying cycle has a non-NULL ``equity_after``.
        """
        cur = self._conn.execute(
            """
            SELECT MAX(equity_after) FROM cycles
            WHERE status IN ('completed','error') AND equity_after IS NOT NULL
            """,
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
