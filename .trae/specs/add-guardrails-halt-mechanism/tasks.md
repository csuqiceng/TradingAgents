# Tasks

- [x] Task 1: 新增配置项（default_config.py）
  - [x] SubTask 1.1: 在 DEFAULT_CONFIG 中新增 `runner_daily_loss_limit = -0.10`
  - [x] SubTask 1.2: 在 DEFAULT_CONFIG 中新增 `runner_max_drawdown = -0.15`
  - [x] SubTask 1.3: 在 `_ENV_OVERRIDES` 新增 `TRADINGAGENTS_RUNNER_DAILY_LOSS_LIMIT` → `runner_daily_loss_limit`
  - [x] SubTask 1.4: 在 `_ENV_OVERRIDES` 新增 `TRADINGAGENTS_RUNNER_MAX_DRAWDOWN` → `runner_max_drawdown`
  - [x] SubTask 1.5: 验证 _coerce 对 float 负数正常工作（reference 是 float，env 字符串 "-0.10" → -0.10）

- [x] Task 2: 新增 state 查询方法（state.py）
  - [x] SubTask 2.1: 实现 `get_day_baseline_equity() -> float | None`：内部算 UTC 今日 [00:00,24:00) epoch 范围，按 `WHERE ts >= ? AND ts < ? AND equity_before IS NOT NULL ORDER BY id ASC LIMIT 1` 查询
  - [x] SubTask 2.2: 实现 `get_peak_equity() -> float | None`：按 `SELECT MAX(equity_after) FROM cycles WHERE status IN ('completed','error') AND equity_after IS NOT NULL` 查询
  - [x] SubTask 2.3: 单测：get_day_baseline_equity 当天有多条 cycle 返回首条非 NULL equity_before
  - [x] SubTask 2.4: 单测：get_day_baseline_equity 首条 equity_before=NULL 时返回下一条非 NULL
  - [x] SubTask 2.5: 单测：get_day_baseline_equity 当天无 cycle 返回 None
  - [x] SubTask 2.6: 单测：get_day_baseline_equity 跨 UTC 日界只返回今日的（不混入昨日）
  - [x] SubTask 2.7: 单测：get_peak_equity 有多条 completed/error 返回最大 equity_after
  - [x] SubTask 2.8: 单测：get_peak_equity 排除 halted cycle（equity_after=None 不计入）
  - [x] SubTask 2.9: 单测：get_peak_equity 含 error cycle 的 equity_after
  - [x] SubTask 2.10: 单测：get_peak_equity 无记录或全为 running 返回 None

- [x] Task 3: 新增守门员检查方法（loop.py）
  - [x] SubTask 3.1: 实现 `_check_guardrails(equity_before: float | None) -> tuple[bool, str]`（无需 trade_date 参数，内部算 UTC）
  - [x] SubTask 3.2: 首次执行校验 `runner_daily_loss_limit < 0` 且 `runner_max_drawdown < 0`，非法值抛 ValueError
  - [x] SubTask 3.3: equity_before 为 None 时返回 (False, "")
  - [x] SubTask 3.4: 内部 try/except 包裹 state 查询，异常时 fail-open 返回 (False, "") + WARNING
  - [x] SubTask 3.5: 调用 get_day_baseline_equity()，为 None 或 <=0 时跳过日内检查；否则算 (equity_before-baseline)/baseline，<=阈值记 reason="daily_loss_limit"
  - [x] SubTask 3.6: 调用 get_peak_equity()，为 None 或 <=0 时跳过回撤检查；否则算 (equity_before-peak)/peak，<=阈值记 reason="max_drawdown"
  - [x] SubTask 3.7: 两个都触发时 reason 用 "+" 连接（如 "daily_loss_limit+max_drawdown"）
  - [x] SubTask 3.8: 都通过返回 (False, "")
  - [x] SubTask 3.9: 单测：日内浮亏触及阈值 halt（-11% <= -10%）
  - [x] SubTask 3.10: 单测：日内浮亏未触及放行（-8% > -10%）
  - [x] SubTask 3.11: 单测：日内恰好等于阈值 halt（-10% <= -10%，闭区间）
  - [x] SubTask 3.12: 单测：历史回撤触及阈值 halt
  - [x] SubTask 3.13: 单测：历史回撤未触及放行
  - [x] SubTask 3.14: 单测：equity_before=None 放行
  - [x] SubTask 3.15: 单测：day_baseline=None 跳过日内检查
  - [x] SubTask 3.16: 单测：peak_equity=None 跳过回撤检查
  - [x] SubTask 3.17: 单测：baseline=0 跳过日内检查（除零保护）
  - [x] SubTask 3.18: 单测：peak=0 跳过回撤检查（除零保护）
  - [x] SubTask 3.19: 单测：两个都触发返回复合 reason
  - [x] SubTask 3.20: 单测：阈值正数抛 ValueError
  - [x] SubTask 3.21: 单测：state 查询异常 fail-open 返回 (False, "")

- [x] Task 4: run_once 顺序调整 + 检查点插入
  - [x] SubTask 4.1: 调整 run_once 顺序：snapshot → stop-loss → guardrail → [halt跳过propagate/订单] → complete_cycle（stop-loss 前置到 guardrail 之前）
  - [x] SubTask 4.2: stop-loss 检查后、propagate 前调用 `_check_guardrails(equity_before)`
  - [x] SubTask 4.3: Halt 时仍调用 start_cycle 记录 cycle（审计痕迹）
  - [x] SubTask 4.4: Halt 时调用 complete_cycle(cycle_id, status="halted", equity_after=None)
  - [x] SubTask 4.5: Halt 时跳过 propagate、订单执行（但不跳过已执行的 stop-loss）
  - [x] SubTask 4.6: Halt 返回 summary 含全部 _log_summary 所需字段：halted, halt_reason, cycle_id, ticker, trade_date, rating=None, order_status="halted", equity_before, equity_after=None, error=None, report_path=None
  - [x] SubTask 4.7: 非 halt 时 run_once 后续流程完全不变
  - [x] SubTask 4.8: 集成测：halt 路径不调用 get_graph / propagate
  - [x] SubTask 4.9: 集成测：halt 路径 stop-loss 已先执行（_check_stop_loss 被调用）
  - [x] SubTask 4.10: 集成测：halt 时 start_cycle 被调用、complete_cycle(status="halted") 被调用、equity_after=None
  - [x] SubTask 4.11: 集成测：非 halt 路径流程不变

- [x] Task 5: run_forever 日志与 cycle_count 处理
  - [x] SubTask 5.1: run_forever 中识别 summary.get("halted")，halted 时日志输出 "cycle halted: {reason}"
  - [x] SubTask 5.2: halted cycle 不 increment cycle_count（避免触发 reflection）
  - [x] SubTask 5.3: halted cycle 后继续 sleep + 下一个 cycle，不中断循环
  - [x] SubTask 5.4: 验证 status 命令能展示 halted cycle（不破坏现有 status 输出）
  - [x] SubTask 5.5: 集成测：连续 halt 后 equity 回升 → 自动恢复交易
  - [x] SubTask 5.6: 集成测：halted cycle 不触发 reflect_on_trades / reflect_on_decisions

- [x] Task 6: 回归测试
  - [x] SubTask 6.1: 运行 `pytest tests/test_runner_state.py tests/test_crypto_execution.py -v` 全通过
  - [x] SubTask 6.2: 全量 `pytest tests/ -v` 无回归
  - [x] SubTask 6.3: 验证代理逻辑（exchange.proxies）未被触碰
  - [x] SubTask 6.4: 验证现货约束（defaultType: 'spot'）未被触碰
  - [x] SubTask 6.5: 验证 stop-loss / cooldown / 反思循环未被破坏

# Task Dependencies

- Task 1 无依赖（配置项独立）
- Task 2 无依赖（state 方法独立）
- Task 1 和 Task 2 可并行
- Task 3 依赖 Task 1（读配置、阈值校验）和 Task 2（调 state 方法）
- Task 4 依赖 Task 3（调 _check_guardrails）
- Task 5 依赖 Task 4（halt summary 契约）
- Task 6 依赖 Task 1-5 全部完成
