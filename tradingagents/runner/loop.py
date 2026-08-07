"""Autonomous trading loop.

Wraps the single-shot ``TradingAgentsGraph.propagate`` in a loop that:

  1. Snapshots the exchange account (balances) and caches it in SQLite.
  2. Runs the full analysis -> PM decision -> broker order pipeline.
  3. After each order, refreshes the affected position from the exchange and
     records the order + cycle to SQLite.
  4. Sleeps ``runner_interval_seconds`` and repeats, across one or more
     tickers, until interrupted or ``runner_max_cycles`` is reached.

The loop is synchronous (one cycle at a time). Crypto execution is spot-only
and the analysis graph is a blocking LangGraph invoke, so there is no benefit
to asyncio here — simplicity and debuggability win.

Design notes:
- The exchange is ALWAYS authoritative for "what do I hold". The SQLite
  positions table is a fast cache + audit log; every cycle starts by
  refreshing from ``get_account_snapshot`` so a drifted row never causes a
  bad decision.
- ``run_once(ticker)`` runs a single cycle for one ticker. ``run_forever()``
  loops over all configured tickers each cycle. ``run_once`` is also the
  entry point for tests and for the "trigger one cycle now" use case.
- Network proxy for yfinance (requests-based) is set from
  ``crypto_https_proxy`` so both the data layer and the broker go through the
  same tunnel. ccxt gets its own ``httpsProxy`` param in the broker.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
import traceback
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.crypto_broker import CryptoBroker, to_ccxt_symbol
from tradingagents.runner.state import RunnerStateStore

logger = logging.getLogger(__name__)

# Tickers that mean crypto. Kept local (not imported from cli.utils) to avoid
# a cli -> tradingagents dependency direction; the runner is a library module.
_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC")


def _is_crypto(ticker: str) -> bool:
    t = ticker.upper().strip()
    return any(t.endswith(s) for s in _CRYPTO_SUFFIXES)


class TradingLoop:
    """Scheduled autonomous trading loop over one or more tickers.

    Parameters mirror the ``runner_*`` / ``crypto_*`` / ``execution_*`` config
    keys. Pass an explicit ``config`` to override; otherwise reads
    ``DEFAULT_CONFIG`` (which already applies ``TRADINGAGENTS_*`` env vars).
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or DEFAULT_CONFIG
        self.tickers = [
            t.strip() for t in str(self.config.get("runner_tickers", "BTC-USD")).split(",")
            if t.strip()
        ]
        self.interval = float(self.config.get("runner_interval_seconds", 3600))
        self.max_cycles = int(self.config.get("runner_max_cycles", 0))

        # SQLite state store. Default path under the data cache dir.
        db_path = self.config.get("runner_db_path") or os.path.join(
            self.config.get("data_cache_dir", os.path.expanduser("~/.tradingagents/cache")),
            "runner_state.db",
        )
        self.store = RunnerStateStore(db_path)

        # Apply proxy env for yfinance (requests-based). ccxt gets its own
        # httpsProxy in the broker constructor; both read from the same config.
        proxy = self.config.get("crypto_https_proxy")
        if proxy:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ.setdefault(k, proxy)

        self._broker: CryptoBroker | None = None
        self._graph = None  # lazily built; imports are heavy (LangGraph)

    # ------------------------------------------------------------------ #
    # Lazy accessors
    # ------------------------------------------------------------------ #

    def get_broker(self) -> CryptoBroker:
        """A shared CryptoBroker for account queries (separate from the one
        the graph builds per-cycle for order placement, but same creds)."""
        if self._broker is None:
            self._broker = CryptoBroker(
                exchange_id=self.config.get("crypto_exchange", "binance"),
                api_key=self.config.get("crypto_api_key"),
                secret=self.config.get("crypto_secret"),
                passphrase=self.config.get("crypto_passphrase"),
                https_proxy=self.config.get("crypto_https_proxy"),
                testnet=self.config.get("execution_mode", "paper") == "paper",
                quote_budget=self.config.get("crypto_quote_budget", 1000.0),
                max_position_fraction=self.config.get("crypto_max_position", 0.2),
                cooldown_seconds=self.config.get("crypto_cooldown_seconds", 14400.0),
            )
        return self._broker

    def get_graph(self):
        """Lazily build the TradingAgentsGraph. Heavy import deferred so
        ``TradingLoop()`` construction is cheap (tests that stub the graph
        don't pay the LangGraph import cost)."""
        if self._graph is None:
            # Lazy import: tradingagents.graph pulls in LangGraph + all agents.
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            # Crypto has no fundamentals data; default to market+social+news.
            analysts = ["market", "social", "news"]
            self._graph = TradingAgentsGraph(analysts, config=self.config, debug=False)
        return self._graph

    # ------------------------------------------------------------------ #
    # Account reconciliation
    # ------------------------------------------------------------------ #

    def snapshot_account(self) -> dict[str, Any]:
        """Pull the live account balance from the exchange and cache every
        non-zero asset as a position row. Returns the snapshot dict."""
        broker = self.get_broker()
        snap = broker.get_account_snapshot()
        if "error" in snap:
            logger.warning("account snapshot failed: %s", snap["error"])
            return snap

        balances = snap.get("balances", {})
        quote = "USDT"
        for base, row in balances.items():
            # We only have a price for assets that trade against the quote
            # currency; for others (e.g. OKB) we store the balance with no
            # price. This is fine — the positions table is a cache, the
            # exchange is authoritative.
            price = None
            try:
                if base != quote:
                    price = broker._fetch_price(f"{base}/{quote}")
            except Exception:
                pass
            self.store.upsert_position_from_snapshot(base, quote, row, price)
        logger.info("account snapshot: %d non-zero assets cached", len(balances))
        return snap

    def refresh_position(self, ticker: str) -> dict[str, Any]:
        """Re-fetch one symbol's position from the exchange after an order,
        and upsert it into the local store. Returns the position dict."""
        broker = self.get_broker()
        pos = broker.get_position(ticker)
        if "error" in pos:
            logger.warning("refresh_position failed for %s: %s", ticker, pos["error"])
            return pos
        self.store.upsert_position(
            symbol=pos["symbol"],
            base=pos["base"],
            quote="USDT",
            free=pos["free"],
            used=pos["used"],
            total=pos["total"],
            price=pos["price"],
            value_quote=pos["value_quote"],
            source="exchange",
        )
        return pos

    # ------------------------------------------------------------------ #
    # Single cycle
    # ------------------------------------------------------------------ #

    def run_once(self, ticker: str) -> dict[str, Any]:
        """Run one complete cycle for ``ticker``.

        Steps:
          1. Snapshot account (equity_before).
          2. propagate(ticker, today) -> PM decision markdown.
          3. _execute_decision -> order result.
          4. Refresh the affected position; record order + cycle.
        Returns a summary dict.
        """
        asset_type = "crypto" if _is_crypto(ticker) else "stock"
        trade_date = dt.date.today().strftime("%Y-%m-%d")

        # 1. Pre-cycle account snapshot (for equity_before + position cache).
        equity_before = None
        try:
            snap = self.snapshot_account()
            equity_before = snap.get("equity_quote") if "error" not in snap else None
        except Exception as exc:
            logger.warning("pre-cycle snapshot failed: %s", exc)

        cycle_id = self.store.start_cycle(ticker, trade_date, equity_before)
        logger.info("=== cycle %d: %s @ %s (%s) ===", cycle_id, ticker, trade_date, asset_type)

        rating: str | None = None
        order_status: str | None = None
        order_result: dict[str, Any] | None = None
        error: str | None = None

        try:
            graph = self.get_graph()
            # 2. Full analysis -> decision markdown.
            # propagate() already calls _execute_decision internally when
            # execution_enabled is true (see trading_graph.py). We must NOT
            # call _execute_decision again here — that would double-execute
            # and the second call would always hit the cooldown the first
            # call just wrote, producing a misleading "skipped" record.
            final_state, decision_md = graph.propagate(ticker, trade_date, asset_type=asset_type)

            # Parse rating for logging/DB (same heuristic the broker uses).
            from tradingagents.agents.utils.rating import parse_rating
            rating = parse_rating(decision_md or "")

            # 3. Read the order result that propagate() already recorded.
            order_result = final_state.get("executed_order")
            if order_result is None:
                # execution_enabled was false, or asset_type not supported.
                if not self.config.get("execution_enabled"):
                    order_result = {"status": "none", "reason": "execution disabled"}
                else:
                    order_result = {
                        "status": "skipped",
                        "reason": f"execution not implemented for asset_type={asset_type!r}",
                    }
            order_status = order_result.get("status")

            # 4. Refresh the position for this symbol from the exchange.
            try:
                self.refresh_position(ticker)
            except Exception as exc:
                logger.warning("post-cycle position refresh failed: %s", exc)

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            logger.error("cycle %d failed: %s", cycle_id, error)

        # Record the order row (even on skip/error — it's an audit log).
        if order_result is not None:
            self.store.record_order(
                cycle_id=cycle_id,
                symbol=order_result.get("symbol"),
                action=order_result.get("action", "HOLD"),
                status=order_result.get("status", "error"),
                rating=rating,
                price=order_result.get("price"),
                amount=order_result.get("amount"),
                reason=order_result.get("reason"),
                exchange_order_id=(order_result.get("order") or {}).get("id"),
                raw=order_result,
            )

        # Post-cycle account snapshot (equity_after).
        equity_after = None
        try:
            snap2 = self.snapshot_account()
            equity_after = snap2.get("equity_quote") if "error" not in snap2 else None
        except Exception:
            pass

        self.store.complete_cycle(
            cycle_id,
            status="error" if error else "completed",
            rating=rating,
            order_status=order_status,
            equity_after=equity_after,
            error=error,
        )

        return {
            "cycle_id": cycle_id,
            "ticker": ticker,
            "trade_date": trade_date,
            "rating": rating,
            "order_status": order_status,
            "equity_before": equity_before,
            "equity_after": equity_after,
            "error": error,
        }

    # ------------------------------------------------------------------ #
    # Loop driver
    # ------------------------------------------------------------------ #

    def run_forever(self) -> None:
        """Run cycles forever (or until ``max_cycles`` reached / interrupted).

        Each cycle iterates over all configured tickers sequentially, then
        sleeps ``interval`` seconds. The interval is measured from the start
        of one cycle to the start of the next, so a slow analysis simply
        extends the wall-clock cadence rather than stacking up.
        """
        logger.info(
            "TradingLoop starting: tickers=%s interval=%ss max_cycles=%s",
            self.tickers, self.interval, self.max_cycles or "inf",
        )
        cycle_count = 0
        try:
            while True:
                for ticker in self.tickers:
                    summary = self.run_once(ticker)
                    self._log_summary(summary)
                    cycle_count += 1
                    if self.max_cycles and cycle_count >= self.max_cycles:
                        logger.info("max_cycles=%d reached, stopping", self.max_cycles)
                        return

                if self.max_cycles and cycle_count >= self.max_cycles:
                    return
                # Sleep until the next cycle. If analysis took longer than
                # interval, we start immediately (no negative sleep).
                time.sleep(max(0, self.interval))
        except KeyboardInterrupt:
            logger.info("interrupted by user, stopping")

    @staticmethod
    def _log_summary(s: dict[str, Any]) -> None:
        print(
            f"[cycle {s['cycle_id']}] {s['ticker']} @ {s['trade_date']} "
            f"-> rating={s['rating']} order={s['order_status']} "
            f"equity={s.get('equity_before')} -> {s.get('equity_after')}"
            + (f" ERROR={s['error'].splitlines()[0]}" if s.get("error") else ""),
            flush=True,
        )

    # ------------------------------------------------------------------ #
    # Status / introspection (for the CLI / debugging)
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        """Return a status snapshot: last cycle + current positions."""
        return {
            "tickers": self.tickers,
            "interval_seconds": self.interval,
            "max_cycles": self.max_cycles,
            "last_cycle": self.store.last_cycle(),
            "positions": self.store.list_positions(),
            "recent_orders": self.store.list_orders(limit=10),
        }
