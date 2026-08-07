"""TradingAgents BTC runner entry point."""
import logging
import sys

from tradingagents.runner.loop import TradingLoop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

if __name__ == "__main__":
    loop = TradingLoop()
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        # Single cycle for testing
        import json
        results = []
        for ticker in loop.tickers:
            results.append(loop.run_once(ticker))
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    else:
        loop.run_forever()
