# Tasks

- [ ] Task 1: 修复 equity 口径（总权益计算）
  - [ ] SubTask 1.1: 修改 `crypto_broker.py` 的 `_quote_equity` 方法，改为计算 quote + base×price
  - [ ] SubTask 1.2: 添加价格获取失败的降级处理（回退 quote-only + warning 日志）
  - [ ] SubTask 1.3: `get_account_snapshot` 返回字段 `equity_quote` 改为 `equity_total`
  - [ ] SubTask 1.4: 单测：构造 {"USDT":{"total":2000},"BTC":{"total":0.05}} + price=60000 → 断言 5000
  - [ ] SubTask 1.5: 单测：价格获取失败 → 回退 quote-only + warning

- [ ] Task 2: Equity 口径版本化（SQLite 迁移）
  - [ ] SubTask 2.1: `state.py` 的 `_MIGRATIONS` 新增 `ALTER TABLE cycles ADD COLUMN equity_version INTEGER DEFAULT 1`
  - [ ] SubTask 2.2: `complete_cycle` 和 `start_cycle` 方法写入 `equity_version=2`（新口径）
  - [ ] SubTask 2.3: 单测：迁移幂等（重复执行不报错）
  - [ ] SubTask 2.4: 单测：旧记录 equity_version=1，新记录 equity_version=2

- [ ] Task 3: 新增配置项
  - [ ] SubTask 3.1: `default_config.py` 新增 `crypto_max_portfolio_exposure=0.4`
  - [ ] SubTask 3.2: `default_config.py` 新增 `crypto_min_cash_ratio=0.25`
  - [ ] SubTask 3.3: `default_config.py` 修改 `crypto_max_position` 默认值 0.2 → 0.15
  - [ ] SubTask 3.4: 添加对应环境变量映射（TRADINGAGENTS_CRYPTO_MAX_PORTFOLIO_EXPOSURE、TRADINGAGENTS_CRYPTO_MIN_CASH_RATIO）
  - [ ] SubTask 3.5: 验证 legacy 环境变量 fallback 不破坏

- [ ] Task 4: 组合总敞口上限检查
  - [ ] SubTask 4.1: `risk.py` 新增 `portfolio_exposure_check(positions, total_equity, max_exposure, side)` 函数
  - [ ] SubTask 4.2: `risk.py` 新增 `min_cash_check(cash_balance, total_equity, min_ratio, side)` 函数
  - [ ] SubTask 4.3: `crypto_broker.py` 的 `_submit` 在 `position_cap_breach` 之后加组合敞口检查和现金比例检查
  - [ ] SubTask 4.4: SELL 单不受组合敞口和现金比例限制（仅 BUY 检查）
  - [ ] SubTask 4.5: 单测：组合敞口 35% 放行 BUY
  - [ ] SubTask 4.6: 单测：组合敞口 42% 拒绝 BUY，返回 skipped + "portfolio_exposure_breach"
  - [ ] SubTask 4.7: 单测：现金比例 20% 拒绝 BUY，返回 skipped + "min_cash_breach"
  - [ ] SubTask 4.8: 单测：SELL 不受限

- [ ] Task 5: PM 持仓快照注入
  - [ ] SubTask 5.1: `loop.py` 的 `run_once` 在调用 graph propagate 前，调用 `list_positions()` 获取持仓
  - [ ] SubTask 5.2: 把持仓快照塞进 graph state（新增 state key `portfolio_snapshot`）
  - [ ] SubTask 5.3: `portfolio_manager.py` 的 PM prompt 新增持仓快照段（positions + 总敞口 + 占比 + 浮盈）
  - [ ] SubTask 5.4: PM prompt 新增仓位软约束提示（接近上限/已超上限两种文案）
  - [ ] SubTask 5.5: 集成测：PM prompt 文本包含持仓快照
  - [ ] SubTask 5.6: 集成测：接近上限时 prompt 包含警告文案

- [ ] Task 6: daily_report 兼容性验证
  - [ ] SubTask 6.1: 检查 `daily_report.py` 是否依赖 `equity_quote` 字段名
  - [ ] SubTask 6.2: 若依赖，更新为 `equity_total`
  - [ ] SubTask 6.3: 验证 daily_report 的 PnL 计算不受 equity 口径变更影响（PnL 用 entry/exit 价格，不用 equity）

- [ ] Task 7: 全量回归测试
  - [ ] SubTask 7.1: 运行 `pytest tests/test_runner_state.py tests/test_crypto_execution.py -v`
  - [ ] SubTask 7.2: 修复因 equity 口径变更导致的测试失败（更新断言值）
  - [ ] SubTask 7.3: 运行全量 `pytest tests/ -v` 确认无回归
  - [ ] SubTask 7.4: 验证代理逻辑（exchange.proxies）未被触碰

# Task Dependencies

- Task 2 依赖 Task 1（equity_version 标记新口径）
- Task 4 依赖 Task 1（组合敞口检查需要正确的 total_equity）和 Task 3（配置项）
- Task 5 依赖 Task 1（持仓占比需要正确的 total_equity）
- Task 6 依赖 Task 1（字段名变更）
- Task 7 依赖 Task 1-6 全部完成
- Task 1 和 Task 3 可并行（无依赖）
