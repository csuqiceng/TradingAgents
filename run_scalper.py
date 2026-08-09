"""Leverage scalper entry point (OKX paper trading, independent process).

Runs the high-frequency 15m scalping strategy on the OKX **simulated**
(sandbox) swap account with 5x leverage and long/short hedge mode. It is a
separate systemd service from ``tradingagents.service`` (the AI spot runner)
and uses its own credentials (``TRADINGAGENTS_SCALPER_*`` env vars — the
simulated account keys, never the live spot keys).

Usage
-----
    python run_scalper.py            # run forever
    python run_scalper.py once       # single scan, then exit (for testing)

Env vars
--------
    TRADINGAGENTS_SCALPER_API_KEY      OKX simulated account API key
    TRADINGAGENTS_SCALPER_SECRET       OKX simulated account secret
    TRADINGAGENTS_SCALPER_PASSPHRASE   OKX simulated account passphrase
    TRADINGAGENTS_SCALPER_PROXY        optional http proxy (default 127.0.0.1:7897)
    TRADINGAGENTS_FEISHU_WEBHOOK       Feishu webhook (shared with AI runner)
"""
from __future__ import annotations

import logging
import os
import sys

import ccxt

from tradingagents.scalper.broker_swap import SwapBroker
from tradingagents.scalper.scalper_loop import ScalperConfig, run_forever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Importing the package triggers dotenv loading (see tradingagents/__init__.py),
# so os.environ picks up TRADINGAGENTS_* vars from .env.


def _build_broker() -> SwapBroker:
    api_key = os.environ.get("TRADINGAGENTS_SCALPER_API_KEY", "")
    secret = os.environ.get("TRADINGAGENTS_SCALPER_SECRET", "")
    passphrase = os.environ.get("TRADINGAGENTS_SCALPER_PASSPHRASE", "")
    proxy = os.environ.get("TRADINGAGENTS_SCALPER_PROXY", "http://127.0.0.1:7897")

    missing = [name for name, val in (
        ("TRADINGAGENTS_SCALPER_API_KEY", api_key),
        ("TRADINGAGENTS_SCALPER_SECRET", secret),
        ("TRADINGAGENTS_SCALPER_PASSPHRASE", passphrase),
    ) if not val]
    if missing:
        raise SystemExit(
            f"Missing scalper credentials in .env: {', '.join(missing)}. "
            "These must be the OKX *simulated* account keys — never reuse the "
            "live spot keys (TRADINGAGENTS_CRYPTO_*)."
        )

    exchange = ccxt.okx({
        "apiKey": api_key,
        "secret": secret,
        "password": passphrase,
        "enableRateLimit": True,
    })
    if proxy:
        exchange.proxies = {"http": proxy, "https": proxy}

    # Hard sandbox — SwapBroker refuses live mode by construction.
    return SwapBroker(exchange, leverage=int(os.environ.get("TRADINGAGENTS_SCALPER_LEVERAGE", "5")), sandbox=True)


def main() -> None:
    if os.environ.get("TRADINGAGENTS_SCALPER_CREDENTIALS_SOURCE") != "env":
        # Explicit guardrail: refuse to fall back to live spot credentials.
        if not os.environ.get("TRADINGAGENTS_SCALPER_API_KEY"):
            raise SystemExit(
                "Refusing to start scalper without explicit simulated credentials. "
                "Set TRADINGAGENTS_SCALPER_API_KEY/SECRET/PASSPHRASE in .env."
            )

    broker = _build_broker()
    config = ScalperConfig(
        interval_seconds=float(os.environ.get("TRADINGAGENTS_SCALPER_INTERVAL", "5")),
    )
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        # Single scan (testing): build store + run one pass.
        from tradingagents.scalper.scalper_loop import ScalperLoop
        from tradingagents.scalper.state_store import ScalperStore
        store = ScalperStore(os.environ.get(
            "TRADINGAGENTS_SCALPER_DB_PATH",
            "/root/.tradingagents/cache/runner_scalper.db",
        ))
        loop = ScalperLoop(broker, store, config)
        loop.run_once()
        print("scalper once complete")
        return

    run_forever(config, broker)


if __name__ == "__main__":
    main()
