# 守门员熔断机制 Checklist

## 配置项
- [x] `runner_daily_loss_limit = -0.10` 配置项 + env 映射 `TRADINGAGENTS_RUNNER_DAILY_LOSS_LIMIT`
- [x] `runner_max_drawdown = -0.15` 配置项 + env 映射 `TRADINGAGENTS_RUNNER_MAX_DRAWDOWN`
- [x] _coerce 对负数 float 正常工作（env "-0.10" → -0.10）

## state 查询方法
- [x] `get_day_baseline_equity()` 实现：UTC 今日 [00:00,24:00) ts 范围 + equity_before IS NOT NULL + ORDER BY id ASC LIMIT 1
- [x] `get_day_baseline_equity()` 跳过 NULL equity_before（取首条非 NULL）
- [x] `get_day_baseline_equity()` 当天无 cycle 返回 None
- [x] `get_day_baseline_equity()` 跨 UTC 日界不混入昨日
- [x] `get_peak_equity()` 实现：status IN ('completed','error') AND equity_after IS NOT NULL 的 MAX
- [x] `get_peak_equity()` 排除 halted cycle（None 天然排除）
- [x] `get_peak_equity()` 排除 running cycle
- [x] `get_peak_equity()` 含 error cycle 的 equity_after
- [x] `get_peak_equity()` 无记录返回 None
- [x] 单测：当天多条 cycle 返回首条非 NULL
- [x] 单测：首条 NULL 返回下一条非 NULL
- [x] 单测：get_peak_equity 返回最大值（含 error cycle）

## 守门员检查方法
- [x] `_check_guardrails(equity_before)` 实现（无需 trade_date 参数，内部算 UTC）
- [x] 首次执行校验 threshold < 0，非法值抛 ValueError
- [x] equity_before=None 返回 (False, "")
- [x] state 查询异常时 fail-open 返回 (False, "") + WARNING
- [x] 日内浮亏 <= 阈值记 reason="daily_loss_limit"
- [x] 历史回撤 <= 阈值记 reason="max_drawdown"
- [x] baseline/peak 为 None 或 <=0 时跳过对应检查（除零保护）
- [x] 两个都触发时 reason 用 "+" 连接
- [x] 都通过返回 (False, "")
- [x] 单测：日内浮亏 halt（-11%）
- [x] 单测：日内恰好等于阈值 halt（-10%，闭区间）
- [x] 单测：日内未触及放行（-8%）
- [x] 单测：历史回撤 halt
- [x] 单测：历史回撤未触及放行
- [x] 单测：equity_before=None 放行
- [x] 单测：无基准放行（day_baseline=None）
- [x] 单测：无峰值放行（peak=None）
- [x] 单测：baseline=0 跳过（除零保护）
- [x] 单测：两个都触发复合 reason
- [x] 单测：阈值正数抛 ValueError
- [x] 单测：state 异常 fail-open

## run_once 顺序与检查点
- [x] run_once 顺序：snapshot → stop-loss → guardrail → [halt跳过propagate/订单] → complete_cycle
- [x] stop-loss 前置到 guardrail 之前（halt 时仍执行）
- [x] guardrail 检查在 stop-loss 后、propagate 前
- [x] Halt 时仍 start_cycle（审计痕迹）
- [x] Halt 时 complete_cycle(status="halted", equity_after=None)
- [x] Halt 时跳过 propagate（不跑 LLM）
- [x] Halt 时跳过订单执行
- [x] Halt summary 含全部 _log_summary 字段：halted, halt_reason, cycle_id, ticker, trade_date, rating=None, order_status="halted", equity_before, equity_after=None, error=None, report_path=None
- [x] 非 halt 时后续流程完全不变
- [x] 集成测：halt 不调用 propagate
- [x] 集成测：halt 时 stop-loss 已先执行
- [x] 集成测：halt 时 start_cycle + complete_cycle(halted) 被调用
- [x] 集成测：非 halt 路径不变

## run_forever 行为
- [x] halted cycle 日志输出 reason
- [x] halted cycle 不 increment cycle_count（不触发 reflection）
- [x] halted cycle 后继续循环（sleep + 下一个 cycle）
- [x] 不引入持久 halt 状态
- [x] 无 halt 冷却
- [x] status 命令展示 halted cycle 不破坏现有输出
- [x] 集成测：连续 halt 后恢复 → 自动恢复交易
- [x] 集成测：halted cycle 不触发 reflection

## 硬约束验证
- [x] 代理逻辑（exchange.proxies）未被触碰
- [x] 现货约束（defaultType: 'spot'）未被触碰
- [x] 冷却机制未被破坏
- [x] stop-loss 逻辑未被破坏（halt 时仍执行）
- [x] 反思循环未被破坏（halted cycle 不参与）
- [x] 无 schema 变更（复用现有 ts/equity_before/equity_after/status 列）
- [x] 无新增过度工程（未加无关配置项/线程/持久状态）

## 回归测试
- [x] `pytest tests/test_runner_state.py tests/test_crypto_execution.py -v` 全通过
- [x] 全量 `pytest tests/ -v` 无回归
