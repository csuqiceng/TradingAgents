"""Real-flow integration test for the guardrails halt mechanism.

Runs the actual TradingLoop code path (real SQLite store, real _check_guardrails,
real run_once halt branch) with a stubbed snapshot_account that simulates
account losses — no exchange connection, no LLM calls, no real orders.

Scenarios:
  1. No history → guardrail passes (first cycle ever).
  2. Seed a high baseline today + high peak → simulate -11% drop → halt.
  3. Halt cycle recorded with status='halted', equity_after=NULL.
  4. Simulate recovery → guardrail passes again.

Run: python tests/test_guardrails_real_flow.py
"""
from __future__ import annotations

import os
import sys
import time
import tempfile
from unittest.mock import patch, MagicMock

# Ensure project root is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tradingagents.runner.loop import TradingLoop
from tradingagents.runner.state import RunnerStateStore
from tradingagents.default_config import DEFAULT_CONFIG


def _make_loop(db_path: str, equity_before: float | None) -> TradingLoop:
    """Build a real TradingLoop with a stubbed snapshot_account."""
    config = dict(DEFAULT_CONFIG)
    config["runner_db_path"] = db_path
    config["runner_tickers"] = "BTC-USD"
    config["runner_daily_loss_limit"] = -0.10
    config["runner_max_drawdown"] = -0.15
    config["execution_enabled"] = False  # don't build a real broker

    loop = TradingLoop(config)
    # Stub snapshot_account to return the simulated equity
    loop.snapshot_account = lambda: {"equity_quote": equity_before} if equity_before is not None else {"error": "simulated snapshot failure"}
    # Stub get_broker so run_once doesn't build a real exchange connection
    fake_broker = MagicMock()
    fake_broker._fetch_price.return_value = 60000.0
    fake_broker.get_position.return_value = None
    loop.get_broker = lambda: fake_broker
    # Stub get_graph so halt path is tested without LLM (halt should skip it anyway)
    loop.get_graph = MagicMock()
    return loop


def _seed_cycle(store: RunnerStateStore, equity_before: float, equity_after: float, status: str = "completed", ts_offset: float = 0):
    """Insert a historical cycle to seed baseline/peak."""
    cycle_id = store.start_cycle("BTC-USD", "2026-01-01", equity_before, 60000.0)
    store.complete_cycle(cycle_id, status=status, rating="HOLD", order_status="none", equity_after=equity_after, error=None, decision_md="# decision", report_path=None)
    # Backdate the ts if needed (for UTC day boundary tests)
    if ts_offset:
        store._conn.execute("UPDATE cycles SET ts = ? WHERE id = ?", (ts_offset, cycle_id))
        store._conn.commit()
    return cycle_id


def main():
    print("=" * 70)
    print("守门员熔断机制 — 真实流程测试")
    print("=" * 70)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="guardrail_test_")
    tmp.close()
    db_path = tmp.name
    print(f"\n[setup] 临时数据库: {db_path}")

    try:
        # ----------------------------------------------------------------
        print("\n--- 场景 1: 无历史记录，首条 cycle 应放行 ---")
        loop = _make_loop(db_path, equity_before=5000.0)
        halted, reason = loop._check_guardrails(5000.0)
        print(f"  _check_guardrails(5000) → halted={halted}, reason={reason!r}")
        assert not halted, "首条 cycle 不应 halt"
        print("  ✅ 放行（无 baseline、无 peak）")

        # ----------------------------------------------------------------
        print("\n--- 场景 2: 植入今日 UTC baseline=5000 + 历史 peak=6000 ---")
        store = loop.store
        # Seed today's baseline (real UTC now via start_cycle)
        seed1 = store.start_cycle("BTC-USD", "2026-01-01", 5000.0, 60000.0)
        store.complete_cycle(seed1, status="completed", rating="HOLD", order_status="none", equity_after=5000.0, error=None, decision_md="", report_path=None)
        # Seed a higher historical peak
        import datetime as _dt
        yesterday = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        seed2 = store.start_cycle("BTC-USD", "2026-01-01", 5900.0, 58000.0)
        store.complete_cycle(seed2, status="completed", rating="BUY", order_status="filled", equity_after=6000.0, error=None, decision_md="", report_path=None)
        store._conn.execute("UPDATE cycles SET ts = ? WHERE id = ?", (yesterday.timestamp(), seed2))
        store._conn.commit()

        baseline = store.get_day_baseline_equity()
        peak = store.get_peak_equity()
        print(f"  get_day_baseline_equity() = {baseline}")
        print(f"  get_peak_equity() = {peak}")
        assert baseline == 5000.0, f"baseline 应为 5000，实际 {baseline}"
        assert peak == 6000.0, f"peak 应为 6000，实际 {peak}"
        print("  ✅ baseline/peak 查询正确")

        # ----------------------------------------------------------------
        print("\n--- 场景 3: 模拟 equity 跌至 4450（日内 -11%、回撤 -25.8%）→ 应 halt ---")
        loop2 = _make_loop(db_path, equity_before=4450.0)
        # Reuse the same store (already seeded)
        loop2.store = store
        halted, reason = loop2._check_guardrails(4450.0)
        print(f"  _check_guardrails(4450) → halted={halted}, reason={reason!r}")
        assert halted, "equity 跌 -11% 应触发 halt"
        assert "daily_loss_limit" in reason, "reason 应含 daily_loss_limit"
        assert "max_drawdown" in reason, "reason 应含 max_drawdown（回撤 -25.8% <= -15%）"
        print(f"  ✅ halt 触发，复合 reason: {reason}")

        # ----------------------------------------------------------------
        print("\n--- 场景 4: 真实 run_once 走 halt 路径（不调 LLM、不下单）---")
        summary = loop2.run_once("BTC-USD")
        print(f"  run_once 返回 summary:")
        for k, v in summary.items():
            print(f"    {k}: {v}")
        assert summary.get("halted") is True, "summary 应 halted=True"
        assert summary.get("order_status") == "halted", "order_status 应为 'halted'"
        assert summary.get("equity_after") is None, "equity_after 应为 None"
        assert summary.get("rating") is None, "rating 应为 None"
        # Verify get_graph (LLM) was NOT called
        loop2.get_graph.assert_not_called()
        print("  ✅ halt 路径正确：LLM 未调用、equity_after=None、order_status=halted")

        # ----------------------------------------------------------------
        print("\n--- 场景 5: 验证 cycles 表记录了 status='halted' ---")
        rows = store._conn.execute(
            "SELECT id, status, equity_before, equity_after, order_status FROM cycles WHERE status='halted' ORDER BY id DESC LIMIT 1"
        ).fetchall()
        assert rows, "cycles 表应有 status='halted' 的记录"
        row = rows[0]
        print(f"  cycles 表 halt 记录: id={row[0]}, status={row[1]}, equity_before={row[2]}, equity_after={row[3]}, order_status={row[4]}")
        assert row[1] == "halted"
        assert row[3] is None, "equity_after 应为 NULL"
        print("  ✅ 审计痕迹正确")

        # ----------------------------------------------------------------
        print("\n--- 场景 6: 模拟 equity 回升至 4600（日内 -8%、回撤 -23.3%）---")
        # 日内 -8% > -10% 放行，但回撤 -23.3% <= -15% 仍 halt
        loop3 = _make_loop(db_path, equity_before=4600.0)
        loop3.store = store
        halted, reason = loop3._check_guardrails(4600.0)
        print(f"  _check_guardrails(4600) → halted={halted}, reason={reason!r}")
        # 日内 -8% 未触 -10%，但回撤 (4600-6000)/6000 = -23.3% <= -15%
        assert halted, "回撤 -23.3% 仍应触发 halt"
        assert reason == "max_drawdown", f"reason 应为 'max_drawdown'，实际 {reason!r}"
        print("  ✅ 日内放行但回撤仍 halt（reason='max_drawdown'）")

        # ----------------------------------------------------------------
        print("\n--- 场景 7: 模拟 equity 回升至 5200（日内 +4%、回撤 -13.3%）→ 放行 ---")
        loop4 = _make_loop(db_path, equity_before=5200.0)
        loop4.store = store
        halted, reason = loop4._check_guardrails(5200.0)
        print(f"  _check_guardrails(5200) → halted={halted}, reason={reason!r}")
        # 日内 (5200-5000)/5000 = +4% 放行，回撤 (5200-6000)/6000 = -13.3% > -15% 放行
        assert not halted, "日内 +4%、回撤 -13.3% 都未触阈值，应放行"
        print("  ✅ 恢复交易（两个检查都通过）")

        # ----------------------------------------------------------------
        print("\n--- 场景 8: equity_before=None（快照失败）→ fail-open 放行 ---")
        loop5 = _make_loop(db_path, equity_before=None)
        loop5.store = store
        halted, reason = loop5._check_guardrails(None)
        print(f"  _check_guardrails(None) → halted={halted}, reason={reason!r}")
        assert not halted, "equity_before=None 应 fail-open 放行"
        print("  ✅ 快照失败时放行（让 stop-loss 兜底）")

        # ----------------------------------------------------------------
        print("\n--- 场景 9: 阈值正数 → ValueError ---")
        loop6 = _make_loop(db_path, equity_before=5000.0)
        loop6.store = store
        loop6.config["runner_daily_loss_limit"] = 0.10  # 误设正数
        try:
            loop6._check_guardrails(5000.0)
            print("  ❌ 应抛 ValueError 但未抛")
            assert False
        except ValueError as e:
            print(f"  ✅ 正数阈值抛 ValueError: {e}")

        print("\n" + "=" * 70)
        print("🎉 全部 9 个真实流程场景通过！")
        print("=" * 70)

    finally:
        try:
            os.unlink(db_path)
            print(f"\n[cleanup] 已删除临时数据库: {db_path}")
        except OSError:
            pass


if __name__ == "__main__":
    main()
