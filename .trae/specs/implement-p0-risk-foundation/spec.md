# P0 风控地基：equity口径修复 + PM持仓注入 + 组合总敞口上限 Spec（v2）

> v2 修订：经3轮agent评审（风控专家/技术架构师/BTC交易员）修正阻断性问题

## 评审修订记录

| 问题 | 来源 | 修订 |
|------|------|------|
| scenario 数学错误（分母5800应为5000） | 风控专家 | 修正 |
| min_cash_ratio 与 max_portfolio_exposure 数学冗余 | 风控专家 | min_cash_ratio 改为独立底线 0.55（>1-0.4=0.6的近似，形成有效分层） |
| 价格失败应 fail-close 而非降级 | 风控专家 | BUY 拒绝(skipped)，SELL 放行 |
| 多 base 资产价格获取策略未定义 | 架构师 | _quote_equity 改签名接收 prices dict，调用方负责取价 |
| graph state 注入需改3处签名 | 架构师 | 改用 past_context 注入（零签名改动，更轻量） |
| loop.py:196,315 的 equity_quote 读取遗漏 | 架构师 | 补入 Task 6 |
| 浮亏导致 equity 下降反而放开 BUY | BTC交易员 | 新增"浮亏禁止加仓"规则（cost basis 检查） |
| SOL 15% 仓位过宽 | BTC交易员 | 按波动率分级：BTC 15% / ETH 12% / SOL 8% |
| 工期 3-4 天不足 | 架构师 | 调整为 6-7 天 |

## Why

当前 TradingAgents 存在四个风控盲区：
1. `crypto_broker.py:396-408` 的 `_quote_equity` 只算 quote 侧（USDT），不算 base 资产（BTC/ETH/SOL）价值，导致仓位上限分母被低估。
2. `portfolio_manager.py:43-67` 的 PM prompt 完全没有持仓信息，PM 在不知已满仓情况下做 BUY 决策。
3. 只有单标的 20% 上限，无组合层总敞口约束。BTC/ETH/SOL 相关性 0.7-0.9，三标的各 20%=60% gross exposure，实际风险过高。
4. **浮亏放松约束**：用实时 market value 算 total_equity 作分母，BTC 下跌时 base value 缩水导致占比下降，反而放开 BUY 限制，形成"越跌越买"的接飞刀风险。

本 spec 修复这四个风控地基问题。

## What Changes

### 1. Equity 口径修复（总权益计算）
- 修改 `crypto_broker.py` 的 `_quote_equity` → `_total_equity`，改签名接收 `prices: dict[str, float]` 参数
- 计算总权益 = quote 现金 + Σ(base 资产×当前价)
- 调用方（`_submit`、`get_account_snapshot`）负责取价并传入
- **价格失败 fail-close**：BUY 返回 skipped(reason="price_unavailable")，SELL 放行（市价单不需预知价格）
- `get_account_snapshot` 返回字段 `equity_quote` 改为 `equity_total`
- `loop.py:196,315` 和 `daily_report.py` 的 `equity_quote` 读取同步改名

### 2. Equity 口径版本化
- `cycles` 表新增 `equity_version` 列（走 `_MIGRATIONS` 幂等迁移，DEFAULT 1）
- 新记录写 `equity_version=2`（新口径），旧记录保留 `equity_version=1`

### 3. 浮亏禁止加仓（解决核心实战问题）
- 新增 `risk.py` 的 `avg_down_check(current_price, entry_price, action)` 函数
- 当 `action=="BUY"` 且 `current_price < entry_price`（浮亏）时，拒绝加仓
- entry_price 从 `state.py` 的 `get_vwap_entry_price(symbol)` 获取（已有方法）
- 无持仓（entry_price 为 None）时不触发，允许首次建仓
- 新增配置 `crypto_no_avg_down=true`（默认启用，可关闭）

### 4. PM 持仓注入（通过 past_context，零签名改动）
- `loop.py` 的 `run_once` 在调用 `graph.propagate` 前，把持仓快照拼成字符串
- 追加到 `past_context` 参数（已有机制，`trading_graph.py:462-464` 在拼 past_context）
- PM prompt 通过 `state.get("past_context", "")` 读取（`portfolio_manager.py:36` 已有）
- 持仓快照内容：positions + 总敞口 + 各标的占比 + 浮动盈亏（从 VWAP entry 算）
- PM prompt 新增软约束提示

### 5. 组合总敞口上限
- 新增配置 `crypto_max_portfolio_exposure=0.4`（40%）
- `risk.py` 新增 `portfolio_exposure_check(positions_value, total_equity, buy_notional, max_exposure, side)`
- 检查口径：BUY 后敞口 = (current_exposure + buy_notional) / total_equity
- BUY 时检查，SELL 不受限
- 遍历所有 base 资产，不硬编码"三标的"

### 6. 最低现金比例（独立底线，与组合上限分层）
- 新增配置 `crypto_min_cash_ratio=0.55`（55%，>1-0.4=0.6 的近似，形成有效分层）
- `risk.py` 新增 `min_cash_check(cash_free, total_equity, buy_notional, min_ratio, side)`
- 检查口径：BUY 后现金 = (cash_free - buy_notional) / total_equity
- BUY 时检查，SELL 不受限
- 注意：与组合上限 40% 形成双约束（敞口≤40% 且 现金≥55%），两者互补不冗余

### 7. 单标的上限按波动率分级
- `crypto_max_position` 改为 dict 配置：`{"BTC-USD": 0.15, "ETH-USD": 0.12, "SOL-USD": 0.08}`
- 向后兼容：若配置为 float，所有标的用统一值
- 保留 `position_cap_breach` 作为单标的硬兜底

### 8. 防线层次明确
- 第一道：PM prompt 软约束（给 PM 上下文）
- 第二道：浮亏禁止加仓（`avg_down_check`）
- 第三道：单标的 cap（`position_cap_breach`，分级 15%/12%/8%）
- 第四道：组合敞口上限（`portfolio_exposure_check`，40%）
- 第五道：最低现金比例（`min_cash_check`，55%）
- 五道防线并存，broker 层是硬兜底

## Impact

- **Affected specs**: P1（止盈+熔断依赖正确的 equity 口径）、P4（组合层 PM 依赖持仓注入）
- **Affected code**:
  - `tradingagents/execution/crypto_broker.py` — equity 口径 + 取价 + 风控检查插入位置
  - `tradingagents/execution/risk.py` — 新增 portfolio_exposure_check / min_cash_check / avg_down_check
  - `tradingagents/agents/managers/portfolio_manager.py` — PM prompt（通过 past_context 读取持仓）
  - `tradingagents/runner/loop.py` — 持仓快照拼接到 past_context + equity_quote 改名
  - `tradingagents/runner/state.py` — cycles 表加 equity_version 列
  - `tradingagents/graph/trading_graph.py` — past_context 拼接持仓快照（已有机制，加内容）
  - `tradingagents/default_config.py` — 新增配置项 + max_position 改 dict
  - `daily_report.py` — equity_quote 改名
- **BREAKING**: `crypto_max_position` 从 float 改为 dict（float 向后兼容）
- **代理逻辑不受影响**：`exchange.proxies` 设置方式不动

## ADDED Requirements

### Requirement: 总权益计算（Total Equity Calculation）

系统 SHALL 计算账户总权益，包含 quote 侧现金和 base 侧资产按当前市价折算的价值。`_quote_equity` 方法改名为 `_total_equity`，签名增加 `prices: dict[str, float]` 参数。

#### Scenario: 持有 BTC 和 USDT 的总权益计算
- **WHEN** 账户有 2000 USDT 和 0.05 BTC，BTC 当前价 60000
- **THEN** 总权益 = 2000 + 0.05×60000 = 5000 USDT

#### Scenario: 持有多个 base 资产的总权益计算
- **WHEN** 账户有 2000 USDT + 0.05 BTC@60000 + 0.5 ETH@3000
- **THEN** 总权益 = 2000 + 3000 + 1500 = 6500 USDT

#### Scenario: 价格获取失败时 fail-close
- **WHEN** 获取 BTC 当前价失败，PM 决策 BUY BTC
- **THEN** BUY 返回 skipped，reason="price_unavailable"
- **AND** SELL 仍可执行（市价单不需预知价格）

#### Scenario: 部分价格失败时降级
- **WHEN** BTC 价格成功、ETH 价格失败，账户同时持有两者
- **THEN** 总权益 = quote + BTC_value（ETH 忽略），记录 warning "ETH price unavailable, excluded from equity"
- **AND** BUY 仍可执行（基于保守的部分 equity）

### Requirement: Equity 口径版本化

系统 SHALL 在 `cycles` 表记录 equity 口径版本。

#### Scenario: 新口径写入
- **WHEN** P0 上线后写入新的 cycle 记录
- **THEN** `equity_version` 列值为 `2`

#### Scenario: 历史数据保留
- **WHEN** 迁移已存在的旧 cycle 记录
- **THEN** `equity_version` 列值为 `1`（DEFAULT 1）

#### Scenario: 幂等迁移
- **WHEN** 迁移在已迁移的数据库上重复执行
- **THEN** 抛 OperationalError 被 try/except 捕获，不破坏数据

### Requirement: 浮亏禁止加仓

系统 SHALL 在 BUY 时检查当前价是否低于持仓均价（VWAP），浮亏时拒绝加仓。

#### Scenario: 浮亏时禁止加仓
- **WHEN** BTC VWAP entry=60000，当前价 55000（浮亏），PM 决策 BUY BTC
- **THEN** BUY 返回 skipped，reason="avg_down_blocked"

#### Scenario: 浮盈时允许加仓
- **WHEN** BTC VWAP entry=60000，当前价 65000（浮盈），PM 决策 BUY BTC
- **THEN** 继续后续风控检查（不在此处拦截）

#### Scenario: 无持仓时允许建仓
- **WHEN** 无 BTC 持仓（entry_price=None），PM 决策 BUY BTC
- **THEN** 继续后续风控检查（首次建仓不受限）

#### Scenario: 关闭浮亏检查
- **WHEN** `crypto_no_avg_down=false`，BTC 浮亏，PM 决策 BUY
- **THEN** 跳过此检查，继续后续风控检查

### Requirement: PM 持仓快照注入（通过 past_context）

系统 SHALL 在 PM 决策前把当前持仓快照通过 `past_context` 注入 PM 的 prompt 上下文。不新增 graph state key，复用现有 `past_context` 机制。

#### Scenario: 持有多个标的时注入
- **WHEN** PM 分析 BTC-USD，当前持有 0.05 BTC（VWAP 60000，现价 60000，价值 3000）+ 2000 USDT，总权益 5000
- **THEN** past_context 包含："当前持仓：BTC 0.05（价值 3000，占比 60%，浮盈 0%），USDT 2000（40%）。总敞口 60%。"

#### Scenario: 无持仓时注入
- **WHEN** PM 分析 BTC-USD，当前无任何 base 资产持仓
- **THEN** past_context 包含："当前无持仓，全部为 USDT 现金。"

### Requirement: PM 仓位软约束提示

系统 SHALL 在 PM 的 past_context 中加入仓位上限软约束提示。

#### Scenario: 接近上限时提示
- **WHEN** BTC 敞口已达 14%（上限 15%），PM 分析 BTC-USD
- **THEN** past_context 包含："当前 BTC 敞口 14%，接近 max_position 上限 15%，若再 BUY 可能超限。"

#### Scenario: 已超限时提示
- **WHEN** BTC 敞口已达 16%（超上限 15%），PM 分析 BTC-USD
- **THEN** past_context 包含："当前 BTC 敞口 16% 已超 max_position 上限 15%，请改判 HOLD 或只 SELL。"

### Requirement: 组合总敞口上限（BUY 后口径）

系统 SHALL 在 broker BUY 下单前检查 BUY 后的组合总敞口，超限时拒绝。检查口径为 BUY 后敞口 = (current_exposure + buy_notional) / total_equity。

#### Scenario: BUY 后敞口未超限时放行
- **WHEN** 当前组合敞口 35%（上限 40%），BUY notional 使敞口增至 38%，PM 决策 BUY BTC
- **THEN** 订单正常提交

#### Scenario: BUY 后敞口超限时拒绝
- **WHEN** 当前组合敞口 38%（上限 40%），BUY notional 使敞口增至 42%，PM 决策 BUY BTC
- **THEN** 订单被拒绝，返回 skipped，reason="portfolio_exposure_breach"

#### Scenario: SELL 不受组合敞口限制
- **WHEN** 当前组合敞口 45%（超上限 40%），PM 决策 SELL BTC
- **THEN** 订单正常提交（SELL 降低敞口，不受限）

#### Scenario: 遍历所有 base 资产
- **WHEN** 账户持有 BTC+ETH+SOL+LTC（4 个 base 资产）
- **THEN** 组合敞口 = (BTC_value + ETH_value + SOL_value + LTC_value) / total_equity，不硬编码标的数量

### Requirement: 最低现金比例（BUY 后口径）

系统 SHALL 在 broker BUY 下单前检查 BUY 后的现金比例，低于阈值时拒绝。检查口径为 BUY 后现金 = (cash_free - buy_notional) / total_equity。

#### Scenario: BUY 后现金比例充足时放行
- **WHEN** 当前现金比例 60%（阈值 55%），BUY notional 使现金降至 58%，PM 决策 BUY BTC
- **THEN** 订单正常提交

#### Scenario: BUY 后现金比例不足时拒绝
- **WHEN** 当前现金比例 56%（阈值 55%），BUY notional 使现金降至 53%，PM 决策 BUY BTC
- **THEN** 订单被拒绝，返回 skipped，reason="min_cash_breach"

#### Scenario: SELL 不受现金比例限制
- **WHEN** 当前现金比例 30%（低于阈值 55%），PM 决策 SELL BTC
- **THEN** 订单正常提交（SELL 增加现金，不受限）

### Requirement: 单标的上限按波动率分级

系统 SHALL 支持按标的配置不同的仓位上限，`crypto_max_position` 可为 dict 或 float。

#### Scenario: dict 配置
- **WHEN** `crypto_max_position={"BTC-USD": 0.15, "ETH-USD": 0.12, "SOL-USD": 0.08}`
- **AND** PM 决策 BUY SOL-USD
- **THEN** SOL 的仓位上限为 8%

#### Scenario: float 配置（向后兼容）
- **WHEN** `crypto_max_position=0.15`（float）
- **AND** PM 决策 BUY 任意标的
- **THEN** 所有标的的仓位上限为 15%

#### Scenario: dict 中未配置的标的
- **WHEN** `crypto_max_position={"BTC-USD": 0.15}`，PM 决策 BUY ETH-USD
- **THEN** ETH-USD 使用默认上限 0.15

## MODIFIED Requirements

### Requirement: 单标的仓位上限判断口径

仓位上限判断 SHALL 使用总权益（quote + base×price）作为分母。BUY 后持仓占比 = (existing_base_value + buy_notional) / total_equity。

#### Scenario: 持有 base 资产时的上限计算
- **WHEN** 账户有 2000 USDT + 0.05 BTC@60000（总权益 5000），max_position=15%
- **AND** existing BTC value = 3000，PM 决策 BUY 800 USDT 的 BTC
- **THEN** BUY 后 BTC 敞口 = (3000+800)/5000 = 76%，超 15% 上限，拒绝

## REMOVED Requirements

### Requirement: _quote_equity 仅算 quote 侧

**Reason**: 口径错误，base 资产价值不计入导致仓位上限分母被低估
**Migration**: 替换为 `_total_equity`（quote + base×price），旧数据通过 equity_version 列标记

### Requirement: 固定单标的上限（所有标的统一）

**Reason**: BTC/ETH/SOL 波动率差异大（BTC 日波 3-5%，SOL 日波 6-10%），统一上限对高波动币种过宽
**Migration**: 改为按波动率分级的 dict 配置，float 向后兼容
