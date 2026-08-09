"""SQLite persistence for the scalper (positions / cooldowns / loss streak).

Uses a **separate database file** (``runner_scalper.db``) so the scalper
never touches the AI runner's ``runner_state.db``. Table names are also
namespaced (``scalper_*``) to stay unambiguous if the files ever share a
connection.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class ScalperStore:
    """Thread-safe SQLite store for scalper state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scalper_positions (
                    symbol TEXT PRIMARY KEY,
                    side TEXT NOT NULL,
                    contracts REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    margin REAL NOT NULL,
                    entry_time REAL NOT NULL,
                    highest REAL NOT NULL,
                    lowest REAL NOT NULL,
                    params TEXT NOT NULL,
                    signals TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scalper_cooldowns (
                    symbol TEXT PRIMARY KEY,
                    until REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scalper_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scalper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    contracts REAL NOT NULL,
                    margin REAL,
                    pnl_usdt REAL,
                    pnl_pct REAL,
                    reason TEXT,
                    regime TEXT
                );
                CREATE TABLE IF NOT EXISTS scalper_regime_log (
                    ts REAL NOT NULL,
                    regime TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ #
    # Positions
    # ------------------------------------------------------------------ #

    def upsert_position(self, pos: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO scalper_positions
                   (symbol, side, contracts, entry_price, margin, entry_time,
                    highest, lowest, params, signals)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pos["symbol"], pos["side"], pos["contracts"], pos["entry_price"],
                    pos["margin"], pos["entry_time"], pos["highest"], pos["lowest"],
                    json.dumps(pos.get("params", {})), json.dumps(pos.get("signals", [])),
                ),
            )

    def delete_position(self, symbol: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM scalper_positions WHERE symbol = ?", (symbol,))

    def get_position(self, symbol: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scalper_positions WHERE symbol = ?", (symbol,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d.get("params") or "{}")
        d["signals"] = json.loads(d.get("signals") or "[]")
        return d

    def all_positions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM scalper_positions").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["params"] = json.loads(d.get("params") or "{}")
            d["signals"] = json.loads(d.get("signals") or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------------ #
    # Cooldowns
    # ------------------------------------------------------------------ #

    def set_cooldown(self, symbol: str, until: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO scalper_cooldowns (symbol, until) VALUES (?, ?)",
                (symbol, until),
            )

    def cooldown_remaining(self, symbol: str, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT until FROM scalper_cooldowns WHERE symbol = ?", (symbol,)
            ).fetchone()
        if row is None:
            return 0.0
        return max(0.0, row["until"] - now)

    # ------------------------------------------------------------------ #
    # Loss streak / pause
    # ------------------------------------------------------------------ #

    def get_state(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM scalper_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO scalper_state (key, value) VALUES (?, ?)",
                (key, value),
            )

    def consecutive_losses(self) -> int:
        raw = self.get_state("consecutive_losses", "0")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def record_trade_result(self, won: bool) -> None:
        losses = self.consecutive_losses()
        self.set_state("consecutive_losses", str(0 if won else losses + 1))

    def reset_loss_streak(self) -> None:
        self.set_state("consecutive_losses", "0")

    def paused_until(self) -> float:
        raw = self.get_state("paused_until", "0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def set_paused_until(self, ts: float) -> None:
        self.set_state("paused_until", str(ts))

    # ------------------------------------------------------------------ #
    # Trades / regime log
    # ------------------------------------------------------------------ #

    def record_trade(self, trade: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO scalper_trades
                   (ts, symbol, side, action, price, contracts, margin, pnl_usdt, pnl_pct, reason, regime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.get("ts", time.time()), trade.get("symbol"), trade.get("side"),
                    trade.get("action"), trade.get("price", 0.0), trade.get("contracts", 0),
                    trade.get("margin"), trade.get("pnl_usdt"), trade.get("pnl_pct"),
                    trade.get("reason"), trade.get("regime"),
                ),
            )

    def record_regime(self, regime: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO scalper_regime_log (ts, regime) VALUES (?, ?)",
                (time.time(), regime),
            )

    def recent_trades(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scalper_trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
