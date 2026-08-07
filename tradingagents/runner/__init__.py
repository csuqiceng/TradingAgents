"""Autonomous trading runner package.

Wraps the single-shot ``TradingAgentsGraph.propagate`` pipeline in a loop that
can run on a schedule, snapshot account state before/after each cycle, and
persist positions + orders to SQLite so the agent knows what it holds.

The runner is intentionally decoupled from the analysis graph: the graph stays
a pure "ticker + date -> decision" function, and the runner owns the loop,
the state store, and the pre/post-trade account reconciliation.
"""
