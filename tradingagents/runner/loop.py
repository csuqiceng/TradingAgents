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
            decision_md=decision_md,
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
        reflect_every = int(self.config.get("runner_reflect_every_n_cycles", 0) or 0)
        reflect_min_age = float(self.config.get("runner_reflect_min_age_hours", 1.0) or 1.0)
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

                # Periodically reflect on filled trades: pull unreflected
                # orders, compute PnL, call the LLM for lessons, and append
                # to the memory log so the next cycle learns from them.
                if reflect_every and cycle_count % reflect_every == 0:
                    logger.info("auto-reflecting on trades (cycle %d)", cycle_count)
                    try:
                        self.reflect_on_trades(min_age_hours=reflect_min_age)
                    except Exception as exc:
                        logger.error("auto-reflection failed: %s", exc)

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
            "recent_reflections": self.store.list_reflections(limit=5),
        }

    # ------------------------------------------------------------------ #
    # Trade reflection — the "learn from actual trades" layer
    # ------------------------------------------------------------------ #

    _TRADE_REFLECTION_PROMPT = (
        "You are a trading agent reviewing a trade you actually executed, now "
        "that some time has passed and the outcome is known.\n"
        "Write 3-5 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
        "Cover in order:\n"
        "1. Was the trade direction correct? (cite the PnL figure)\n"
        "2. Was the entry timing good, or did you buy high / sell low?\n"
        "3. Which part of the original investment thesis held or failed?\n"
        "4. One concrete, actionable lesson for the next trade on this asset.\n\n"
        "Be specific and honest. Your output will be stored and re-read by "
        "future analysis runs, so every word must earn its place."
    )

    def reflect_on_trades(self, min_age_hours: float = 0, max_trades: int = 10) -> list[dict[str, Any]]:
        """Review filled trades that haven't been reflected on yet.

        For each unreflected filled order:
          1. Fetch the current price from the exchange.
          2. Compute PnL (entry vs current, for BUY) or opportunity cost (for SELL).
          3. Call the LLM to generate a 3-5 sentence reflection + lesson.
          4. Store the reflection in SQLite (trade_reflections table).
          5. Append the lesson to the TradingMemoryLog so the next analysis
             run on the same ticker can read it via get_past_context().

        Returns the list of reflection summaries (newest last).
        """
        broker = self.get_broker()
        pending = self.store.get_filled_orders_for_reflection(min_age_hours=min_age_hours)
        if not pending:
            logger.info("reflect_on_trades: no pending trades to reflect on")
            return []

        # Build the LLM lazily (reuse the graph's quick-thinking model so we
        # share the same provider/key as the analysis layer).
        llm = self._get_reflection_llm()

        results: list[dict[str, Any]] = []
        for trade in pending[:max_trades]:
            sym = trade.get("symbol") or ""
            action = trade.get("action") or ""
            entry_price = trade.get("entry_price")
            amount = trade.get("amount") or 0
            decision_md = trade.get("decision_md") or "(decision text not recorded)"

            # Fetch current price.
            try:
                current_price = broker._fetch_price(sym)
            except Exception as exc:
                logger.warning("reflect: can't fetch price for %s: %s", sym, exc)
                current_price = None

            # Compute PnL.
            # For BUY: PnL = (current - entry) * amount  (unrealized, assumes still holding)
            # For SELL: PnL = (entry - current) * amount  (opportunity saved/given up
            #           by selling — positive means selling was the right call)
            pnl_quote = None
            pnl_pct = None
            if entry_price and current_price and amount:
                if action == "BUY":
                    pnl_quote = (current_price - entry_price) * amount
                    cost = entry_price * amount
                    pnl_pct = (pnl_quote / cost * 100) if cost else None
                elif action == "SELL":
                    # SELL "PnL" = how much we saved (or lost) by selling vs holding
                    pnl_quote = (entry_price - current_price) * amount
                    proceeds = entry_price * amount
                    pnl_pct = (pnl_quote / proceeds * 100) if proceeds else None

            holding_hours = (time.time() - trade.get("order_ts", time.time())) / 3600

            # Call the LLM for reflection.
            reflection_text = "(LLM unavailable — reflection skipped)"
            lessons = None
            if llm is not None:
                try:
                    pnl_desc = "unknown"
                    if pnl_pct is not None:
                        pnl_desc = f"{pnl_pct:+.2f}%"
                    prompt_body = (
                        f"Trade: {action} {amount} {sym} @ {entry_price}\n"
                        f"Current price: {current_price}\n"
                        f"PnL: {pnl_desc} ({pnl_quote:+.2f} quote units "
                        f"if holding{' (opportunity cost for SELL)' if action == 'SELL' else ''})\n"
                        f"Hours since trade: {holding_hours:.1f}\n\n"
                        f"Original decision that drove this trade:\n{decision_md}"
                    )
                    from langchain_core.messages import HumanMessage, SystemMessage
                    msg = llm.invoke([
                        SystemMessage(content=self._TRADE_REFLECTION_PROMPT),
                        HumanMessage(content=prompt_body),
                    ])
                    reflection_text = msg.content if hasattr(msg, "content") else str(msg)
                    # Extract the last sentence as the "lesson" for quick injection.
                    sentences = [s.strip() for s in reflection_text.split(".") if s.strip()]
                    lessons = sentences[-1] + "." if sentences else reflection_text
                except Exception as exc:
                    logger.error("reflect: LLM call failed: %s", exc)
                    reflection_text = f"(LLM reflection failed: {exc})"

            # Store in SQLite.
            self.store.record_reflection(
                order_id=trade.get("order_id"),
                cycle_id=trade.get("cycle_id"),
                ticker=trade.get("ticker", sym),
                action=action,
                rating=trade.get("rating"),
                entry_price=entry_price,
                current_price=current_price,
                amount=amount,
                pnl_quote=pnl_quote,
                pnl_pct=pnl_pct,
                holding_hours=holding_hours,
                decision_md=decision_md,
                reflection_text=reflection_text,
                lessons=lessons,
            )

            # Write back to the TradingMemoryLog so the next analysis run
            # can read the trade lesson via get_past_context().
            self._append_trade_lesson_to_memory(
                ticker=trade.get("ticker", sym),
                action=action,
                sym=sym,
                pnl_pct=pnl_pct,
                lessons=lessons or reflection_text,
            )

            summary = {
                "order_id": trade.get("order_id"),
                "ticker": trade.get("ticker", sym),
                "action": action,
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "reflection": reflection_text[:120] + "..." if len(reflection_text) > 120 else reflection_text,
            }
            results.append(summary)
            self._log_reflection(summary)

        return results

    def _get_reflection_llm(self):
        """Return the LLM to use for trade reflection.

        Reuses the graph's quick-thinking LLM (same provider/key as analysis)
        so we don't need a separate config. Falls back to None if the graph
        can't be built (e.g. missing API key), in which case reflection
        records PnL but skips the LLM prose.
        """
        try:
            graph = self.get_graph()
            return graph.quick_thinking_llm
        except Exception as exc:
            logger.warning("can't build LLM for reflection: %s", exc)
            return None

    def _append_trade_lesson_to_memory(
        self, ticker: str, action: str, sym: str, pnl_pct: float | None, lessons: str
    ) -> None:
        """Append a trade-reflection entry to the TradingMemoryLog.

        Uses a tag format compatible with the existing parser so
        get_past_context() picks it up on the next run. The entry is written
        as already-resolved (not pending) since the outcome is known.
        """
        try:
            from pathlib import Path
            log_path = Path(self.config.get("memory_log_path", "")).expanduser()
            if not log_path:
                return
            log_path.parent.mkdir(parents=True, exist_ok=True)

            pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "n/a"
            tag = f"[{dt.date.today().isoformat()} | {ticker} | TRADE:{action} | {pnl_str} | 0% | 0d]"
            entry = (
                f"{tag}\n\n"
                f"DECISION:\n(Trade reflection: {action} {sym}, PnL {pnl_str})\n\n"
                f"REFLECTION:\n{lessons}\n\n"
                f"<!-- ENTRY_END -->\n\n"
            )
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
            logger.info("trade lesson appended to memory log: %s %s %s", ticker, action, pnl_str)
        except Exception as exc:
            logger.warning("failed to append trade lesson to memory log: %s", exc)

    @staticmethod
    def _log_reflection(s: dict[str, Any]) -> None:
        pnl = f"{s['pnl_pct']:+.2f}%" if s.get("pnl_pct") is not None else "n/a"
        print(
            f"[reflection] order={s['order_id']} {s['ticker']} "
            f"{s['action']} entry={s.get('entry_price')} now={s.get('current_price')} "
            f"pnl={pnl}",
            flush=True,
        )
