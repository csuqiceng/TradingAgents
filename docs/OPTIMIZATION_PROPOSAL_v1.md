# TradingAgents 优化方案 v1

> 基于全面代码调研（20+文件）+ 第一轮专业评审修订

## 项目现状

### 已实现能力

| 维度 | 能力 | 关键位置 |
|------|------|----------|
| 数据采集 | OHLCV + 技术指标(11选8) + 新闻 + 社交情绪 + 宏观(FRED) + Polymarket | dataflows/ |
| 决策 | Bull/Bear辩论 → Research Manager → Trader → 3方风险辩论 → PM终裁 | graph/setup.py:104-152 |
| 执行 | ccxt spot市价单 + 冷却 + 仓位上限 | crypto_broker.py:161-214 |
| 风控 | VWAP硬止损(10%) + 方向冷却(buy 4h/sell 1h) + 仓位上限(20%) | loop.py:431-487 |
| 学习 | 双层反思：交易反思(每3 cycle) + HOLD反思(每18 cycle) + Phase B延迟反思(5日) | loop.py:506-806 |
| 记忆 | Markdown日志 + 5同/3跨ticker教训注入PM prompt | memory.py:70-95 |
| 运维 | SQLite审计 + 飞书日报 + 报告树持久化 + checkpoint/resume | state.py |

### 关键缺失

**组合管理维度**：
- 无资产配置逻辑 — BTC/ETH/SOL等权独立决策
- 无再平衡机制
- 无相关性管理 — 三标的相关性0.7-0.9
- 无现金/稳定币管理
- 无业绩归因

**BTC实战维度**：
- 无BTC-native数据（链上/资金费率/恐惧贪婪/DXY）
- 4h止损延迟
- 无确定性规则固化

**其他遗漏（评审补充）**：
- 无止盈机制（最严重）
- equity口径错误（只算quote侧）
- 无滑点/手续费建模
- LLM prompt injection风险
- 交易所单点风险
- 无黑天鹅熔断

## 优化方案（按优先级）

### P0: PM持仓注入 + equity口径修复 + prompt硬约束（2-3天）

**问题**：portfolio_manager.py:43-67 的PM prompt完全没有持仓信息，PM在不知道已满仓的情况下做BUY决策。

**方案**：
1. 修复 equity 口径：crypto_broker.py:396-408 的 _quote_equity 只算quote侧，改为算总权益(quote + base×price)
2. 注入持仓快照到PM prompt：positions + 总敞口 + 各标的占比 + 浮动盈亏
3. 加prompt硬约束："当前BTC敞口已达X%，若再BUY将超max_position上限，请改判HOLD或只SELL"
4. 迭代测试PM是否遵守约束

**预期效果**：PM决策时能看到仓位，避免越涨越买

### P1: 止盈机制（3-4天）

**问题**：全项目Grep take_profit|trailing|oco 零匹配。系统只有止损没有止盈，买入后只能等止损或PM主动SELL。

**方案**：
1. 新增配置项 crypto_take_profit_pct（默认20%）
2. 新增配置项 crypto_trailing_stop_pct（默认10%，启用追踪止损）
3. 在 _check_stop_loss 旁加 _check_take_profit 逻辑
4. 触发止盈时调 hard_stop_sell（复用现有逻辑，绕过冷却）
5. 追踪止损：记录持仓期间最高价，从最高价回撤超阈值触发

**预期效果**：上涨时自动锁定利润，不是只能等回撤给止损打掉

### P2: 交易所原生止损单（5-7天）

**问题**：4h检查间隔下，BTC 10分钟可跌10%，止损实际是"4h后的10%"。

**方案**：
1. 先实测OKX testnet是否支持algo order（OKX用triggerPx/orderPx，不是Binance的stopPrice）
2. BUY成交后挂交易所stop-loss单
3. 加仓/减仓后取消旧止损单、挂新止损单
4. 保留代码层止损作兜底

**注意**：
- OKX的止损是algo order，走/api/v5/trade/order-algo接口
- ccxt的create_order默认走普通单接口，需要特殊参数
- testnet对conditional order支持可能不完整，必须实测
- 若testnet不支持，保留代码层4h兜底，不强行做

### P3: BTC-native数据源（1.5-2周）

**问题**：当前数据源完全为股票设计，BTC实战关键数据一个都没有。

**方案**（修正后，2/3零新增依赖）：
1. DXY → 走已接入的FRED（DTWEXBGS贸易加权美元指数），零新增依赖
2. 资金费率 → 走ccxt fetchFundingRate（项目已依赖ccxt，Binance/Bybit公共接口免费）
3. 恐惧贪婪指数 → alternative.me（免费无key）
4. 新建crypto_onchain_analyst或注入news_analyst（加asset_type=="crypto"守卫，避免污染股票分析）

**注意**：
- Coinglass已迁移到v4，需API key，非免费，不用
- yfinance DX-Y.NYB是ETF不是ICE DXY，有跟踪误差，用FRED更准

### P4: 组合层PM / PortfolioRebalancer（2-3周）

**问题**：当前是"3个独立单标的策略"，不是"1个组合策略"。

**方案**：
1. 在单标的PM之上加一层PortfolioRebalancer
2. 定期（如每日）评估组合drift
3. 决定再平衡动作（如"减BTC 5%加SOL 5%"）
4. 在loop.py每日首轮运行

**前置依赖**：P0完成

### P5: 事件驱动回测框架（4-6周）

**问题**：当前只有事后反思，无法离线验证策略变更。

**方案**：
- 不要vectorbt（LLM不可复现、不可向量化）
- 自建事件驱动回测 + 决策缓存
- 用历史K线 + 历史新闻快照喂LLM
- hash输入→缓存输出，解决可复现性

### 暂缓/砍掉

| 方案 | 状态 | 原因 |
|------|------|------|
| M3 链上数据+鲸鱼监控 | 暂缓 | Glassnode/CryptoQuant付费贵，先用免费数据验证alpha |
| M4 独立风控线程 | 砍掉 | 过度工程，破坏loop.py:12-14同步设计原则，P2做对后多余 |
| L1 业绩归因(Brinson) | 暂缓 | 对3现货币种过重，先扩展现有reflection层做轻量归因 |
| L2 多策略+信号融合 | 暂缓 | 当前单策略PM决策质量未验证，谈多策略为时过早 |
| L3 做空/永续合约 | 砍掉 | 违背crypto_broker.py:1-7的spot-only硬约束 |

## 评审补充的遗漏点（待纳入）

1. **止盈缺失**（最严重）— 已纳入P1
2. **滑点/手续费未建模** — 全市价单无滑点保护，待规划
3. **LLM prompt injection** — 社交数据被污染可误导下单，待规划
4. **交易所单点风险** — 全仓一个所，宕机无法平仓，待规划
5. **黑天鹅熔断** — 无24h跌幅>20%暂停机制，待规划
6. **equity口径错误** — 已纳入P0

## 执行节奏

- 第1-2周：P0 + P1（风控基础完善）
- 第3-4周：P2 + P3（执行优化 + 数据源扩展）
- 第2-3月：P4 + P5（组合管理 + 回测能力）
