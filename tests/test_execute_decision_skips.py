"""Tests for TradingAgentsGraph._execute_decision skip paths.

The full crypto path is covered by ``test_crypto_execution.py`` using a fake
exchange. Here we verify the two branches that short-circuit *before* the
broker is constructed, so they don't need ccxt installed:

- non-crypto asset types are skipped (only crypto is wired to a broker),
- an unrecognized rating string is skipped rather than crashing the run.

Both must return ``status="skipped"`` and never raise, so a misconfigured
execution flag can't void an otherwise-complete analysis.
"""

from __future__ import annotations

import pytest

from tradingagents.agents.schemas import PortfolioRating
from tradingagents.default_config import DEFAULT_CONFIG


@pytest.mark.unit
class TestExecuteDecisionSkips:
    @pytest.fixture()
    def graph(self, mock_llm_client):
        # Avoids real LLM construction; the graph is never run, only used as a
        # host for _execute_decision + its config dict.
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        # Execution must be enabled for _execute_decision to be called at all.
        config["execution_enabled"] = True
        return TradingAgentsGraph(config=config)

    def test_stock_asset_type_is_skipped(self, graph):
        result = graph._execute_decision(
            "AAPL", "stock", "**Rating**: Buy\n\n**Executive Summary**: ..."
        )
        assert result["status"] == "skipped"
        assert "stock" in result["reason"]
        assert result["rating"] == PortfolioRating.BUY.value

    def test_unrecognized_rating_is_skipped(self, graph):
        # A markdown body with no parseable rating word falls back to "Hold"
        # via parse_rating's default, which IS a valid rating — so to exercise
        # the unrecognized branch we feed garbage and patch parse_rating.
        from unittest.mock import patch

        with patch(
            "tradingagents.graph.trading_graph.parse_rating",
            return_value="NotARealRating",
        ):
            result = graph._execute_decision("BTC-USD", "crypto", "garbage")
        assert result["status"] == "skipped"
        assert "NotARealRating" in result["reason"]

    def test_hold_rating_on_crypto_routes_through_broker(self, graph, tmp_path, monkeypatch):
        # A Hold rating still constructs the broker (the broker is the single
        # place that decides action from rating), but the broker's place_order
        # returns skipped. We verify the wiring by injecting a stub broker and
        # asserting its result is returned verbatim — no ccxt required.
        import tradingagents.execution as execution_pkg

        captured = {}

        class _StubBroker:
            def __init__(self, *args, **kwargs):
                captured["kwargs"] = kwargs

            def place_order(self, symbol, decision, **kw):
                captured["symbol"] = symbol
                captured["rating"] = decision.rating
                return {"status": "skipped", "reason": "broker says hold", "action": "HOLD"}

        monkeypatch.setattr(execution_pkg, "CryptoBroker", _StubBroker)
        result = graph._execute_decision(
            "BTC-USD", "crypto", "**Rating**: Hold\n\n(no action)"
        )
        assert result["status"] == "skipped"
        assert captured["symbol"] == "BTC-USD"
        assert captured["rating"] == PortfolioRating.HOLD
        # Paper mode (default) must request a testnet broker.
        assert captured["kwargs"]["testnet"] is True
