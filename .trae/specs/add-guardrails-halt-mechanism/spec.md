# 守门员熔断机制（Guardrails Halt）Spec（v2）

> v2 修订：经3轮agent评审（风控专家/技术架构师/BTC交易员）修正阻断性问题

## 评审修订记录

| 问题 | 来源 | 修订 |
|------|------|------|
| halt 跳过 stop-loss 造成反身性死亡螺旋 | 三方共识阻断 | halt 时仍执行 stop-loss，调整 run_once 顺序：snapshot→stop-loss→guardrail→[halt跳过propagate/订单] |
| 除零风险（baseline/peak=0） | 风控专家阻断 | baseline/peak<=0 时跳过对应检查 |
| halt summary 字段不完整导致 _log_summary KeyError | 架构师阻断 | halt summary 含全部 _log_summary 所需字段 |
| trade_date 时区口径未定义（BTC 7×24） | BTC交易员阻断 | get_day_baseline_equity 改用 UTC 时间戳范围查，不改全局 trade_date |
| _check_guardrails 签名缺 trade_date（午夜竞态） | 架构师重要 | 改为内部算 UTC 范围，无需外部传 trade_date |
| get_day_baseline_equity 首条 NULL 污染全天 | 风控专家重要 | 跳过 NULL，取首条非 NULL equity_before |
| get_peak_equity 排除 error cycle 导致 peak 偏低 | 风控专家重要 | 改为 status IN ('completed','error') AND equity_after IS NOT NULL |
| 阈值符号无校验，误设正数永久 halt | 风控专家重要 | 配置加载时校验 threshold<0，非法值启动报错 |
| _check_guardrails 异常未处理 | 风控专家重要 | 内部 try/except，异常时 fail-open + WARNING |
| halt reason 优先级丢信息 | 风控专家重要 | 同时触发返回复合 reason |
| halted cycle 触发 reflection（跑 LLM 与 halt 语义冲突） | 风控专家遗漏 | run_forever 中 halted cycle 不 increment cycle_count |
| equity vs 价格口径错配 | BTC交易员重要 | 保留 equity 级阈值（用户已确认），spec 补充说明对应价格波动参考 |
| peak 永久 halt / 无 hysteresis 抖动 / equity_before=None 放行 | BTC交易员重要 | 最小化：保留全历史 peak + fail-open，spec 补充已知限制，后续迭代再优化 |

## Why

当前 `TradingLoop.run_once` 每个 cycle 无条件跑 LLM 分析并下单。BTC/ETH/SOL 4h 波动可能很大，一旦出现连续浮亏或历史回撤加深，继续交易只会放大损失。项目此前已确认需要一个"守门员"风控层：在 cycle 开头检查账户整体盈亏状态，超过阈值时跳过本 cycle（不跑 LLM、不下新单），但**不跳过 stop-loss**（持仓级止损是确定性安全网，账户级熔断时更该运行，不能禁用），也不做额外出场卖出（spot-only + 非确定性 LLM 信号下强制卖出风险更高）。

本 spec 把这个已确认的设计落地为最小实现：两个阈值（日内浮亏 / 历史回撤）、两个 state 查询方法、一个 loop 检查方法，无新线程、无持久化 halt 状态、无 halt 冷却（前序讨论已确认移除冷却以简化）。

**已知限制（最小化阶段暂不处理，后续迭代评估）**：
- equity 级阈值在低仓位下偏松：-10% equity 浮亏在组合总敞口 40% 配置下需三标的同跌约 25% 才触发。这是用户已确认的阈值，保留；若实测偏松可在配置层调小。
- peak 用全历史 max，单次插针冲高后可能长期 halt。当前用 halt 连续日志 + 后续加 feishu 告警缓解，不引入滚动窗口（避免持续阴跌永不触发的反向风险）。
- 无 hysteresis，阈值边缘可能 halt-恢复-halt 抖动。遵循"移除冷却"的设计意图，暂不加迟滞带。
- equity_before=None 时 fail-open（放行），交易所 API 持续故障时风控失效。当前依赖 stop-loss 兜底，后续可加连续失败计数。

## What Changes

### 1. 新增配置项（default_config.py）
- `runner_daily_loss_limit = -0.10`：日内浮亏阈值（负数）。当日 equity 相对日内基准跌幅 ≤ 此值时 halt。
- `runner_max_drawdown = -0.15`：历史回撤阈值（负数）。当日 equity 相对历史峰值回撤 ≤ 此值时 halt。
- 对应 env 映射：`TRADINGAGENTS_RUNNER_DAILY_LOSS_LIMIT`、`TRADINGAGENTS_RUNNER_MAX_DRAWDOWN`。
- 阈值用负数（与"浮亏"语义一致），比较时统一 `equity_change_pct <= threshold`。
- **阈值校验**：`_check_guardrails` 首次执行时校验 `threshold < 0`，非法值（>=0）抛 `ValueError`，防止误设正数导致永久 halt。**阈值校验必须在 fail-open 的 try/except 之外**（见下文异常处理），避免 ValueError 被 fail-open 吞掉导致守门员静默失效。

### 2. 新增 state 查询方法（state.py）
- `get_day_baseline_equity() -> float | None`：返回**今日 UTC 自然日**首条 `equity_before IS NOT NULL` 的 cycle 的 equity_before。方法内部计算 UTC 今日 [00:00, 24:00) 的 epoch 时间戳范围，按 `WHERE ts >= ? AND ts < ? AND equity_before IS NOT NULL ORDER BY id ASC LIMIT 1` 查询。今日无记录或首条非 NULL 记录不存在时返回 None。
  - 用 UTC 而非本地时区：BTC 7×24 市场无本地交易日概念，UTC 是唯一可复现口径。
  - 用 `ts`（epoch 秒）而非 `trade_date` 字符串：避免依赖 run_once 的本地时区 trade_date，守门员口径独立于报告/反思的日期聚合。
  - 跳过 NULL：防止首条 cycle 快照失败（equity_before=NULL）污染全天 baseline。
- `get_peak_equity() -> float | None`：返回所有 `status IN ('completed','error')` 的 cycle 中 `equity_after` 的最大值（`AND equity_after IS NOT NULL`）。无记录返回 None。
  - 含 error cycle：error cycle 的 equity_after 测量值有效（LLM 失败但快照成功），排除会让 peak 偏低。
  - 排除 halted cycle：halted cycle 的 equity_after=None（IS NOT NULL 天然排除）。
  - 排除 running cycle：未完成的 cycle 不应计入峰值。

### 3. 新增守门员检查（loop.py）
- 新增 `_check_guardrails(equity_before: float | None) -> tuple[bool, str]` 方法。
- **无需外部传 trade_date**：方法内部用 UTC 计算 baseline 查询范围。
- 计算口径（halt reason 可复合）：
  - 若 equity_before 为 None（快照失败）：两个检查都跳过，返回 `(False, "")`（fail-open，让 stop-loss / cooldown 兜底）。
  - 首次执行时校验 `runner_daily_loss_limit < 0` 且 `runner_max_drawdown < 0`，非法值抛 ValueError（校验在 fail-open 的 try/except 之外）。
  - 日内浮亏 = (equity_before - day_baseline) / day_baseline；当 day_baseline 为 None 或 <=0 时跳过；否则 `<= runner_daily_loss_limit` 时记 reason="daily_loss_limit"。
  - 历史回撤 = (equity_before - peak_equity) / peak_equity；当 peak_equity 为 None 或 <=0 时跳过；否则 `<= runner_max_drawdown` 时记 reason="max_drawdown"。
  - 两个检查都通过 → 返回 `(False, "")`；任一触发 → 返回 `(True, reason)`；**两个都触发 → reason 用 "+" 连接**（如 "daily_loss_limit+max_drawdown"），便于上层判断严重程度。
- **异常处理**：内部对 state 查询 try/except（仅包裹 get_day_baseline_equity / get_peak_equity 调用），异常时 fail-open（返回 `(False, "")`）并记录 WARNING，防止 guardrail 自身崩溃炸掉 run_once。**阈值校验不在此 try/except 范围内**，确保配置错误不被静默吞掉。

### 4. run_once 执行顺序调整（关键修订）
原顺序：snapshot → start_cycle → stop-loss → propagate → 订单 → complete_cycle
新顺序：**snapshot → stop-loss → guardrail → [halt 则跳过 propagate/订单] → complete_cycle**

具体：
- equity_before 快照后，**先执行 stop-loss 检查**（持仓级确定性止损，不受账户级熔断影响）。
- stop-loss 之后、propagate 之前，执行 `_check_guardrails(equity_before)`。
- **Halt 时**：仍调用 `start_cycle` 记录 cycle（审计痕迹），但跳过 propagate（不跑 LLM）、订单执行、**post-cycle snapshot**（省一次 exchange API 调用，与 halt 省钱语义一致），用 `complete_cycle(cycle_id, status="halted", equity_after=None)` 收尾。equity_after=None 的理由是 halt 时未跑完整流程且跳过了 post-cycle snapshot，测量口径与其他 cycle 不一致。
- **Halt summary 必须包含 `_log_summary` 所需全部字段**：`{halted: True, halt_reason: reason, cycle_id, ticker, trade_date, rating: None, order_status: "halted", equity_before, equity_after: None, error: None, report_path: None}`，避免 run_forever 的 `_log_summary` 因 `[]` 访问 KeyError。

### 5. run_forever 行为
- Halt 不中断循环，不引入持久 halt 状态。下一个 cycle 重新检查，若恢复则自动恢复交易。
- 不加 halt 冷却（前序讨论已确认移除）。
- **halted cycle 不 increment cycle_count**：run_forever 中检查 `summary.get("halted")`，若 True 则跳过 `cycle_count += 1`，避免 halted cycle 触发 reflection（reflection 会跑 LLM，与 halt 省钱语义冲突）。
- halted cycle 日志输出 "cycle halted: {reason}"。

## Impact

- **Affected specs**: P0（equity 口径修复后，equity_before/after 才能真实反映盈亏，守门员才有意义；但本 spec 不强依赖 P0，equity_quote 口径下守门员仍可工作，只是精度略低）。
- **Affected code**:
  - `tradingagents/default_config.py` — 新增 2 个配置项 + 2 个 env 映射。
  - `tradingagents/runner/state.py` — 新增 2 个查询方法（无 schema 变更，复用现有 cycles 表 ts/equity_before/equity_after/status 列）。
  - `tradingagents/runner/loop.py` — 新增 `_check_guardrails`，调整 run_once 顺序（stop-loss 前置到 guardrail 之前），run_forever 跳过 halted cycle 的 cycle_count 自增。
- **不影响**: 代理逻辑（exchange.proxies）、现货约束（defaultType: 'spot'）、冷却机制、反思循环（halted cycle 不参与）、stop-loss 逻辑（halt 时仍执行）。
- **非 BREAKING**：纯新增，无字段改名，无 schema 变更。

## ADDED Requirements

### Requirement: 日内浮亏熔断

系统 SHALL 在每个 cycle 开头检查当日 equity（UTC 自然日）相对日内基准的浮亏，超过阈值时跳过本 cycle 的 LLM 分析与下单（但不跳过 stop-loss）。

#### Scenario: 日内浮亏触及阈值时 halt
- **WHEN** UTC 今日基准 equity=5000，当前 equity_before=4450（跌幅 -11%），`runner_daily_loss_limit=-0.10`
- **THEN** 返回 `(True, "daily_loss_limit")`，cycle 以 status=halted 结束，不跑 LLM、不下新单，但 stop-loss 已先执行。

#### Scenario: 日内浮亏未触及阈值时放行
- **WHEN** UTC 今日基准 equity=5000，当前 equity_before=4600（跌幅 -8%），`runner_daily_loss_limit=-0.10`
- **THEN** 返回 `(False, "")`，cycle 正常执行。

#### Scenario: UTC 今日首条 cycle 无基准时跳过日内检查
- **WHEN** UTC 今日还没有任何 cycle 记录（day_baseline=None）
- **THEN** 日内浮亏检查跳过（不触发 halt），仅看历史回撤检查。

#### Scenario: 首条 cycle equity_before=NULL 时取下一条非 NULL
- **WHEN** UTC 今日首条 cycle 的 equity_before=NULL（快照失败），第二条 cycle equity_before=5000
- **THEN** `get_day_baseline_equity()` 返回 5000（跳过 NULL），不污染全天 baseline。

### Requirement: 历史回撤熔断

系统 SHALL 在每个 cycle 开头检查当前 equity 相对历史峰值 equity_after 的回撤，超过阈值时跳过本 cycle。

#### Scenario: 历史回撤触及阈值时 halt
- **WHEN** 历史峰值 equity_after=6000，当前 equity_before=5050（回撤 -15.83%），`runner_max_drawdown=-0.15`
- **THEN** 返回 `(True, "max_drawdown")`，cycle 以 status=halted 结束。

#### Scenario: 历史回撤未触及阈值时放行
- **WHEN** 历史峰值 equity_after=6000，当前 equity_before=5200（回撤 -13.3%），`runner_max_drawdown=-0.15`
- **THEN** 返回 `(False, "")`，cycle 正常执行。

#### Scenario: 无历史记录时跳过回撤检查
- **WHEN** 数据库无任何 completed/error cycle（peak_equity=None）
- **THEN** 历史回撤检查跳过，仅看日内浮亏检查。

#### Scenario: error cycle 的 equity_after 计入峰值
- **WHEN** 历史 completed cycle equity_after=5500，error cycle equity_after=6000
- **THEN** `get_peak_equity()` 返回 6000（含 error cycle 的有效测量值）。

#### Scenario: halted cycle 的 equity_after 不计入峰值
- **WHEN** halted cycle equity_after=None，completed cycle equity_after=5500
- **THEN** `get_peak_equity()` 返回 5500（IS NOT NULL 排除 halted 的 None）。

### Requirement: 阈值符号校验

系统 SHALL 在守门员首次执行时校验阈值为负数，非法值（>=0）启动即报错。

#### Scenario: 误设正数阈值启动报错
- **WHEN** `TRADINGAGENTS_RUNNER_DAILY_LOSS_LIMIT=0.10`（误设正数）
- **THEN** `_check_guardrails` 首次执行抛 `ValueError`，提示阈值必须为负数。

### Requirement: 除零保护

系统 SHALL 在 baseline 或 peak 为 0/负数时跳过对应检查，不抛 ZeroDivisionError。

#### Scenario: baseline=0 时跳过日内检查
- **WHEN** UTC 今日首条非 NULL equity_before=0（空账户），当前 equity_before=0
- **THEN** 日内浮亏检查跳过（不除零），仅看历史回撤检查。

### Requirement: equity_before 缺失时保守放行

系统 SHALL 在 equity_before 快照失败（None）时跳过两个熔断检查，让 stop-loss / cooldown 兜底。

#### Scenario: 快照失败时不 halt
- **WHEN** equity_before=None（snapshot_account 抛异常或返回 error）
- **THEN** 两个熔断检查都跳过，返回 `(False, "")`，cycle 继续执行（stop-loss 仍运行）。

### Requirement: guardrail 异常 fail-open

系统 SHALL 在 state 查询抛异常时 fail-open，不因 guardrail 自身崩溃炸掉 run_once。

#### Scenario: SQLite 异常时放行
- **WHEN** `get_day_baseline_equity()` 抛 sqlite3.OperationalError（DB 锁）
- **THEN** `_check_guardrails` 捕获异常，记录 WARNING，返回 `(False, "")`，cycle 继续执行。

### Requirement: halt reason 复合

系统 SHALL 在两个熔断同时触发时返回复合 reason，便于上层判断严重程度。

#### Scenario: 日内+历史同时触发
- **WHEN** 日内浮亏 -11% 且历史回撤 -16%，两个阈值都触及
- **THEN** 返回 `(True, "daily_loss_limit+max_drawdown")`。

### Requirement: Halt 时仍执行 stop-loss

系统 SHALL 在 halt 之前先执行 stop-loss 检查。halt 只跳过 LLM propagate 和新订单执行，不跳过持仓级止损。

#### Scenario: halt 时 stop-loss 已先执行
- **WHEN** equity_before 触发 daily_loss_limit，但某持仓已跌破 VWAP stop-loss
- **THEN** stop-loss 先触发强平（记录 stop-loss 订单），随后 guardrail 检查 halt，cycle 以 status=halted 结束（不再跑 LLM/新订单）。

### Requirement: Halt 不写入 equity_after

系统 SHALL 在 halt 的 cycle 不写入 equity_after（保持 None），避免污染历史峰值统计。

#### Scenario: Halt cycle 的 equity_after 为 None
- **WHEN** cycle 因 daily_loss_limit 被 halt
- **THEN** `complete_cycle` 调用时 equity_after=None，cycles 表该行 equity_after 列为 NULL，不参与后续 `get_peak_equity` 计算。

### Requirement: Halt summary 字段完整

系统 SHALL 在 halt 返回的 summary 中包含 `_log_summary` 所需的全部字段，避免 run_forever KeyError。

#### Scenario: halt summary 字段齐全
- **WHEN** cycle 被 halt
- **THEN** 返回 summary 包含 `halted, halt_reason, cycle_id, ticker, trade_date, rating=None, order_status="halted", equity_before, equity_after=None, error=None, report_path=None`，`_log_summary` 可正常输出。

### Requirement: Halt 不中断 run_forever 且不触发 reflection

系统 SHALL 在 halt 后继续 run_forever 循环，下一个 cycle 重新检查。halted cycle 不 increment cycle_count，不触发 reflection。

#### Scenario: 恢复后自动恢复交易
- **WHEN** cycle N 因浮亏 halt，cycle N+1 时 equity 回升超过阈值
- **THEN** cycle N+1 正常执行 LLM 与下单，无需人工复位。

#### Scenario: halted cycle 不触发 reflection
- **WHEN** cycle N 被 halt，cycle_count 原本应触发 reflection（每 N cycle）
- **THEN** halted cycle 不 increment cycle_count，reflection 不触发（避免 halt 时跑 LLM 与省钱语义冲突）。

## MODIFIED Requirements

### Requirement: run_once 执行流程

`run_once` SHALL 按"snapshot → stop-loss → guardrail → [halt 跳过 propagate/订单] → complete_cycle"顺序执行。stop-loss 前置到 guardrail 之前，确保持仓级止损不受账户级熔断影响。

#### Scenario: 正常 cycle 流程
- **WHEN** 守门员检查通过（未 halt）
- **THEN** run_once 后续流程（stop-loss 已执行 → propagate → 订单 → complete_cycle）不变。

#### Scenario: Halt 时的流程
- **WHEN** 守门员检查返回 `(True, reason)`
- **THEN** stop-loss 已先执行；跳过 propagate、跳过订单执行，调用 `complete_cycle(cycle_id, status="halted", equity_after=None)`，返回完整 summary。

## REMOVED Requirements

无。本 spec 为纯新增。
