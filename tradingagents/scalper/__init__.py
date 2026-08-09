"""Leverage scalper module (OKX swap, paper trading).

A self-contained high-frequency scalping strategy that runs in its own
loop/process, completely independent from the AI analyst team
(``tradingagents.runner.loop``). It re-implements the logic of the legacy
Cryptocurrency/scalp-trader (15m K-line trend + signal + volume scoring,
5x leverage, long/short, market-regime adaptive parameters) with fresh code,
on the OKX *paper* (simulated) account.

Module layout
-------------
- ``indicators.py``   — technical indicators (ADX/RSI/MACD/EMA/Bollinger/vol ratio)
- ``market_regime.py``— bull / bear / ranging classification + param switching
- ``strategy.py``     — 8-category signal scoring + 4-gate entry confirmation
- ``broker_swap.py``  — OKX swap execution (ccxt): leverage, hedge mode, TP/SL
- ``state_store.py``  — SQLite persistence (positions / cooldowns / losses)
- ``scalper_loop.py`` — high-frequency scheduling loop

None of the existing tradingagents modules are modified by this package.
"""
