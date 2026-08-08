# TradingAgents 优化方案（最终版）

> 经3轮agent评审（传统基金经理/BTC实战交易员/技术架构师）达成一致
> 文档路径：docs/OPTIMIZATION_PROPOSAL_FINAL.md

## 评审过程

| 轮次 | 评审视角 | 核心贡献 |
|------|----------|----------|
| 第1轮 | 综合（基金经理+BTC交易员+架构师合一） | 识别6大缺失，提出S1-S3短期方案 |
| 第2轮 | 专业分工（3个独立agent并行） | 发现止盈遗漏、equity口径bug、OKX代码错误、优先级重排 |
| 第3轮 | 三方独立评审v1方案 | 参数改ATR、回测提前、熔断进P1、组合敞口进P0 |

## 三方达成的共识

1. **P0方向正确**：equity口径修复 + PM持仓注入是该最先做的
2. **止盈缺失是结构性缺陷**：和止损同等重要，必须补
3. **固定%参数不对**：BTC年化波动率40-60%，固定止损10%会被噪音扫掉，应改ATR自适应
4. **回测应提前**：定参数前必须有回测验证，哪怕最小版
5. **黑天鹅熔断应进P1**：但24h定义对BTC太慢，应是1h短窗口
6. **组合总敞口上限应进P0**：相关性0.7-0.9，3×20%=60%实际风险过高
7. **砍掉M4独立线程**：破坏同步设计原则
8. **砍掉L3做空/永续**：违背spot-only硬约束

## 三方分歧与解决

| 分歧 | 基金经理 | BTC交易员 | 架构师 | 最终解决 |
|------|----------|-----------|--------|----------|
| 止损参数 | 2×ATR20 | 2.5×ATR(14,4h) | 未评论 | **2×ATR(14,4h)，下限8%** |
| 止盈参数 | trailing 3×ATR | 分批：1/3固定+2/3 trailing | 未评论 | **分批止盈：1/3在2R，2/3用3×ATR trailing** |
| 回测优先级 | P1.5（最小版1周） | 暂缓但警告信息泄露 | 8-10周 | **拆分：P1.5最小版+P5完整框架** |
| 熔断定义 | 24h组合回撤>15% | 1h跌幅>X% | 未提 | **1h跌幅>8%或4h跌幅>12%立即减仓50%** |
| 数据源 | 务实OK | 补OI+Coinbase Premium | 注意spot defaultType | **补OI，加Coinbase Premium，降权FNG** |

---

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

### 关键缺失（三方共识）

**风控维度**：
- 无止盈机制（最严重）— 买入后只能等止损或PM主动SELL
- equity口径错误 — crypto_broker.py:396-408只算quote侧不算base资产
- 无组合总敞口上限 — 3标的各20%=60%，相关性0.7-0.9下实际风险过高
- 无黑天鹅熔断 — 无短时间暴跌的紧急减仓机制
- 固定%止损不适应BTC波动率结构

**组合管理维度**：
- 无资产配置逻辑 — BTC/ETH/SOL等权独立决策，不是组合
- 无再平衡机制
- 无相关性管理
- 无现金/稳定币管理

**BTC实战维度**：
- 无BTC-native数据（链上/资金费率/OI/Coinbase Premium/DXY）
- 无周末效应/时段流动性感知
- 无减半周期定位
- 4h止损延迟（BTC 10分钟可跌10%）

**执行维度**：
- 全市价单无滑点保护
- 无手续费建模（PnL计算不含手续费）
- 交易所单点风险

**安全维度**：
- LLM prompt injection风险（社交数据被污染）
- 双触发冲突（交易所止损+代码止损可能双卖）

---

## 优化方案（最终优先级）

### P0: equity口径修复 + PM持仓注入 + 组合总敞口上限（3-4天）

**问题**：
1. crypto_broker.py:396-408 的 _quote_equity 只算quote侧，BTC涨的时候base value不计入equity，仓位上限分母被低估
2. portfolio_manager.py:43-67 的PM prompt完全没有持仓信息，PM在不知已满仓情况下做BUY决策
3. 只有单标的20%上限，无组合层总敞口约束，3标的各20%=60%gross，相关性0.7-0.9下实际风险过高

**方案**：
1. **修复equity口径**：改为 quote + base×price（总权益）
2. **equity版本化**：cycles表加 equity_version 列，避免历史数据与新口径混淆（走_MIGRATIONS幂等迁移）
3. **持仓注入PM**：在loop.py的run_once前把list_positions()结果塞进graph state，PM prompt显示positions + 总敞口 + 各标的占比 + 浮动盈亏
4. **prompt软约束**："当前BTC敞口已达X%，若再BUY将超max_position上限，请改判HOLD或只SELL"
5. **组合总敞口上限**：新增配置 crypto_max_portfolio_exposure=0.4（40%），在broker层硬执行（三标的合计敞口≤40%）
6. **明确防线层次**：prompt是软提示，position_cap_breach（crypto_broker.py:256）+ 组合敞口检查是硬兜底，两者并存
7. **轻量VaR暂缓说明**：基金经理建议P0加轻量VaR（历史模拟法，90天窗口，95/99分位）+ 3个压力场景（2020-3-12、2022-5 LUNA、2022-11 FTX）。经三方讨论，VaR需要历史波动率数据仓库，P0工期3-4天无法容纳，**暂缓至P5回测框架建成后评估**。P0阶段用组合总敞口上限40%作为替代风控措施。

**参数变更**：
- 单标的仓位上限：20% → 15%（三方共识，压低单一标的风险）
- 新增：组合crypto总敞口上限 40%
- 新增：最低现金/USDT比例 25%（保留抄底子弹）

**预期效果**：PM决策时能看到仓位且不被单一资产类别风险放大

**测试策略**：
- 单测_quote_equity新口径：构造{"USDT":{"total":2000},"BTC":{"total":0.05}} + price=60000 → 断言5000
- 单测position_cap_breach在新口径下的行为：持仓已60%时拒绝加仓
- 单测组合敞口上限：三标的合计>40%时拒绝加仓
- 集成测PM prompt渲染：断言positions快照出现在prompt文本中
- 在线观察期：跑10+ cycle统计PM在cap边缘的决策是否符合约束

### P1: 止盈机制 + 黑天鹅熔断（ATR自适应）（5-6天）

**问题**：
1. 全项目Grep take_profit|trailing|oco 零匹配，只有止损没有止盈
2. 固定%止损10%对BTC过紧（年化波动率40-60%，单日5-8%波动常见），会被噪音频繁触发
3. 无黑天鹅熔断，BTC黑天鹅是小时级事件（2024.8.5四小时-15%），24h统计是马后炮

**方案**：

**1. ATR自适应止损止盈**（替代固定%）
- 新增配置 crypto_use_atr_stops=true（启用ATR自适应，false回退固定%）
- ATR周期：14根4h K线
- 止损：2×ATR(14,4h)，下限8%（防极端低波时止损过紧）
- 止盈：分批制
  - 第一批：1/3仓位在 +2R（2倍止损距离）固定止盈
  - 第二批：2/3仓位用 3×ATR(14,4h) 追踪止损（从最高价回撤）
- 追踪止损需记录持仓期间最高价（HWM）

**2. HWM（最高价）管理**
- positions表新增 high_water_mark 列（走_MIGRATIONS迁移）
- 每次snapshot_account刷新价格时更新 HWM = max(old_hwm, new_price)
- position清零时重置HWM=None（否则下次开仓会立即触发追踪止损）
- **关键**：upsert_position_from_snapshot的ON CONFLICT DO UPDATE必须保留HWM，不能被覆盖

**3. 止盈后不stamp buy cooldown**
- 止损触发后stamp buy cooldown（防"接飞刀"）— 保持现有逻辑
- 止盈触发后只stamp sell cooldown，不stamp buy cooldown（止盈是获利了结，不是逃生）

**4. 黑天鹅熔断**
- 触发条件：1h跌幅>8% 或 4h跌幅>12%（短窗口，非24h）
- 触发动作：立即减仓50% + 暂停交易4h
- 新增配置 crypto_circuit_breaker_1h_pct=8.0, crypto_circuit_breaker_4h_pct=12.0
- 实现位置：_check_stop_loss旁加_check_circuit_breaker

**5. state.py schema变更**
- positions表加 high_water_mark REAL 列
- 走_MIGRATIONS幂等迁移

**参数变更**：
| 参数 | 旧值 | 新值 | 理由 |
|------|------|------|------|
| 止损 | 10%固定 | 2×ATR(14,4h)，下限8% | 适应BTC波动率结构 |
| 止盈 | 无 | 1/3在2R + 2/3用3×ATR trailing | 分批锁定利润 |
| 熔断 | 无 | 1h跌>8%或4h跌>12%减仓50% | BTC黑天鹅是小时级 |
| buy冷却 | 4h | 4h（不变） | — |
| sell冷却 | 1h | 1h（不变） | — |

**预期效果**：上涨时自动锁定利润，黑天鹅时紧急减仓保命

**测试策略**：
- 单测_check_take_profit：固定entry、模拟current价格，断言触发/不触发
- 单测HWM更新：模拟价格序列[100,120,110,130]，断言HWM=130
- 单测HWM在position清零后重置：total=0 → HWM=None
- 单测upsert_position不丢失既有HWM
- 单测ATR计算：给定K线序列，断言ATR值
- 单测黑天鹅熔断：1h跌8%触发、4h跌12%触发、未达阈值不触发
- 回归测：止盈触发后buy cooldown不被stamp（区别于止损）

### P1.5: 最小回测harness（1周）

**问题**：P1的ATR参数需要回测验证，不能拍脑袋定

**方案**：
1. **不回放LLM**（LLM不可复现+信息泄露问题）
2. 只回放历史K线 + ATR止损止盈规则 + 简单统计
3. 输出：总收益、最大回撤、胜率、盈亏比、止损触发次数、止盈触发次数
4. 用BTC/ETH/SOL过去6个月4h K线回放
5. 目标：验证ATR参数合理性，不是验证LLM策略

**技术实现**：
- 新建 backtest/minimal_harness.py
- 用yfinance拉历史K线（已有依赖）
- 模拟entry（随机或固定时间点）→ 跑ATR止损止盈逻辑 → 统计PnL
- 不需要LLM、不需要新闻快照、不需要决策缓存

**预期效果**：P1参数有数据支撑，不是凭直觉

**注意**：LLM回测的信息泄露问题（模型已知后续走势）本阶段不涉及，因为不回放LLM

### P2: 交易所原生止损 + 执行质量 + 双触发idempotency（7-10天）

**问题**：
1. 4h检查间隔下止损延迟（BTC 10分钟可跌10%）
2. 全市价单无滑点保护（SOL等薄流动性book滑点可能0.5-2%）
3. 交易所止损+代码止损可能双卖

**方案**：

**1. 交易所原生止损单**
- 先实测OKX testnet是否支持algo order
- OKX用 triggerPx + orderPx 参数，走 /api/v5/trade/order-algo 接口（不是Binance的stopPrice/type:stop_market）
- ccxt通过unified createOrder带triggerPrice参数支持
- BUY成交后挂交易所stop-loss单
- 加仓/减仓后取消旧止损单、挂新止损单
- OKX algo单有数量限制（单symbol约20-30个活跃单），需先取消旧单再挂新单
- 锁定ccxt版本下限（requirements.txt）

**2. 执行质量**
- 大单改限价单 + 滑点上限保护
- 新增配置 crypto_max_slippage_pct=0.5（滑点超0.5%拒绝成交）
- 在reflection层PnL计算中计入手续费（taker fee 0.1%）

**3. 双触发idempotency**
- 代码层_check_stop_loss前先查交易所止损单是否还在
- 交易所已触发后代码层不重复卖
- 止损完成后撤掉交易所止损单

**4. testnet不支持时的fallback**
- 保留代码层4h止损作兜底
- 不强行做交易所止损单

**5. 折中方案（如果testnet不支持algo order）**
- 不缩短cycle间隔（保持4h，不破坏同步架构）
- 在time.sleep(interval)期间插短间隔（5分钟）价格轮询，只查价格不跑LLM，触发硬止损
- 注意ccxt限流

**预期效果**：止损从4h延迟降到秒级，滑点可控，无双卖

**测试策略**：
- testnet实测：挂algo stop order → 推进价格 → 断言触发
- mock ccxt测createOrder的params透传（triggerPrice正确映射）
- fallback测：testnet不支持时降级到代码层止损
- 双触发idempotency测：交易所已触发后代码层不重复卖
- 滑点保护测：限价单超出滑点上限时拒绝成交

**testnet滑点限制声明**：testnet流动性不真实，仅用于验证algo order触发机制，**不可用于滑点评估**。滑点保护通过线上配置 crypto_max_slippage_pct 兜底，滑点参数需在主网极小金额（如$50）验证后确定。

### P3: BTC-native数据源 + 市场结构感知（2周）

**问题**：
1. 当前数据源完全为股票设计，BTC实战关键数据一个都没有
2. 无周末效应/时段流动性感知
3. 无减半周期定位

**方案**（修正后，2/4零新增依赖）：

**1. 数据源**（按优先级）
- **DXY** → 走已接入的FRED（DTWEXBGS贸易加权美元指数），零新增依赖
- **资金费率+OI** → 走ccxt fetchFundingRate + fetchOpenInterest（项目已依赖ccxt）
  - 注意：spot defaultType下可能需要params={'type':'swap'}或单独实例
  - OI比funding领先：funding转负+OI收缩=多头平仓，funding转负+OI扩张=空头加仓
- **Coinbase Premium** → Coinbase vs Binance价差（美国机构资金流向，BTC顶底领先信号）
- **恐惧贪婪指数** → alternative.me（免费无key，但是慢变量，4h下基本是噪音，降权使用）
- **暂缓**：Coinglass（付费）、Glassnode/CryptoQuant链上数据（付费贵）

**2. 注入方式**
- 新建crypto_onchain_analyst或注入news_analyst
- 加asset_type=="crypto"守卫，避免污染股票分析
- FNG作为风险调节器（极端恐惧→谨慎买入），不作为主信号

**3. 周末效应/时段感知**
- PM prompt注入当前是周末/工作日、亚洲/欧洲/美洲时段
- 周末funding经常失真，PM应降权周末funding信号
- 周末插针概率高，PM应更保守

**4. 减半周期上下文**
- PM prompt注入当前距上次减半月数
- 历史规律：减半后12-18个月通常见顶
- 当前：2024.4减半，处于减半后约16个月（牛市中后段）

**5. CME Gap（BTC特有）**
- 周一开盘检查CME Gap
- 缺口回补概率70%+
- 作为短期均值回归信号

**预期效果**：PM有BTC市场结构感知，不再是"用股票思维交易BTC"

**测试策略**：
- 每个数据源独立mock测（HTTP/ccxt）
- asset_type守卫测：crypto数据不注入股票analyst
- 资金费率接口在spot defaultType下的兼容性测
- 周末/时段判断测
- 减半周期计算测

### P4: 组合层PM / PortfolioRebalancer（3-4周，需先出架构设计文档）

**问题**：当前是"3个独立单标的策略"，不是"1个组合策略"

**前置依赖**：P0完成 + P1.5最小回测验证参数

**方案**：

**1. 架构选择**
- 不在graph内部加组合层node（破坏per-ticker graph独立性）
- 在loop.py run_forever层加组合层裁决
- 每日首轮跑完所有ticker单标的分析后，再跑一次组合层裁决
- 保持sequential以不破坏SQLite单写锁假设

**2. 目标权重方法论**
- 波动率平价（risk parity）：按1/vol反比分配
- SOL vol >> BTC vol，等权会让SOL贡献绝大部分组合波动
- 波动率平价让每个标的贡献等量风险

**3. 再平衡触发条件**
- drift>20%或周度择优触发（不是daily，避免换手成本）
- crypto日波动大，daily rebalance换手成本高（maker/taker fee 0.1%×2=20bps/次）

**4. 相关性管理**
- 组合层crypto beta总敞口≤40%（P0已加）
- 相关性0.7-0.9意味着"分散"是幻觉，大跌时相关性趋近1

**5. 现金/稳定币管理**
- 最低现金/USDT比例25%（P0已加）
- 保留抄底子弹

**6. 组合层输出**
- 生成rebalance指令（如"减BTC 5%加SOL 5%"）
- 回写到per-ticker决策：覆盖单标的PM决策或生成额外rebalance order

**预期效果**：从"3个独立策略"升级为"1个组合策略"

**测试策略**：
- 组合层rebalance逻辑单测：构造drift>阈值的组合，断言再平衡动作
- 多ticker组合视角测试：3标的相关性下的减仓加仓决策
- 与单标的PM的集成测：组合层不破坏单标的graph的独立可跑性
- 现金/稳定币管理测

### P5: 完整事件驱动回测框架（8-10周）

**问题**：当前只有事后反思，无法离线验证策略变更

**方案**：

**1. 不要vectorbt**
- LLM不可复现（同prompt不同调用结果不同）
- 不可向量化（每次决策是一次LLM调用，耗时数秒到数十秒）
- 依赖外部数据快照（新闻、funding当时值）

**2. 事件驱动回测 + 决策缓存**
- 用历史K线 + 历史新闻快照喂LLM
- hash输入→缓存输出，解决可复现性
- 固定provider + temperature=0

**3. hash输入设计（核心难题）**
- K线/技术指标数据：可hash
- 新闻快照：可hash
- 社交情绪：可hash
- 辩论历史：可hash但体积大
- past_context（含历史反思lessons）：**hash不稳定根源**
  - 每次反思后变化，缓存命中率趋零
  - 解决方案：hash输入不含past_context，但标注"忽略反思影响"的回测失真程度
  - 或做时间分桶的模型隔离

**4. 信息泄露处理**
- LLM回测时已知后续走势（训练数据泄露）
- 至少做时间分桶的模型隔离
- 或用更弱的模型做回测
- 否则回测曲线会好得不可信

**5. 历史数据仓库**
- 历史K线 + 历史新闻快照需要对齐到同一时间戳
- 项目当前dataflows是实时拉取，无历史快照存储
- 需先建历史数据仓库

**预期效果**：策略迭代可量化对比，A/B测试不同prompt/参数

**测试策略**：
- hash稳定性测：相同输入→相同hash
- 历史数据回放测：给定历史K线序列，回放决策
- 缓存命中率测：跑100个历史时点，统计命中率
- 回测vs实盘一致性测：同一时点的回测决策与实盘记录的决策对比

---

## 砍掉/暂缓的方案

| 方案 | 状态 | 原因 | 三方共识 |
|------|------|------|----------|
| M3 链上数据+鲸鱼监控 | 暂缓 | Glassnode/CryptoQuant付费贵，先用免费数据验证alpha | 基金经理同意暂缓；BTC交易员建议保留免费替代（mempool/whale-alert） |
| M4 独立风控线程 | 砍掉 | 过度工程，破坏loop.py:12-14同步设计原则，P2做对后多余 | 三方一致同意砍掉 |
| L1 业绩归因(Brinson) | 暂缓 | 对3现货币种过重，先扩展现有reflection层做轻量归因 | 基金经理同意暂缓 |
| L2 多策略+信号融合 | 暂缓 | 当前单策略PM决策质量未验证，谈多策略为时过早 | 三方一致同意暂缓 |
| L3 做空/永续合约 | 砍掉 | 违背crypto_broker.py:1-7的spot-only硬约束 | 三方一致同意砍掉 |

---

## 评审补充的遗漏点处理

| 遗漏点 | 处理 | 纳入哪个P |
|--------|------|-----------|
| 止盈缺失（最严重） | 已纳入 | P1 |
| equity口径错误 | 已纳入 | P0 |
| 组合总敞口上限 | 已纳入 | P0 |
| 黑天鹅熔断 | 已纳入 | P1 |
| 滑点/手续费未建模 | 已纳入 | P2 |
| 双触发idempotency | 已纳入 | P2 |
| 止盈后不stamp buy cooldown | 已纳入 | P1 |
| HWM在upsert时保留 | 已纳入 | P1 |
| equity口径版本化 | 已纳入 | P0 |
| 周末效应/时段感知 | 已纳入 | P3 |
| 减半周期定位 | 已纳入 | P3 |
| CME Gap | 已纳入 | P3 |
| LLM prompt injection | 待规划 | 未来 |
| 交易所单点风险 | 待规划 | 未来 |
| LLM回测信息泄露 | 已纳入 | P5 |

---

## 参数变更总表

| 参数 | 旧值 | 新值 | 理由 | 纳入P |
|------|------|------|------|-------|
| 单标的仓位上限 | 20% | 15% | 压低单一标的风险 | P0 |
| 组合crypto总敞口上限 | 无 | 40% | 相关性0.7-0.9下必须约束总暴露 | P0 |
| 最低现金/USDT比例 | 无 | 25% | 保留抄底子弹 | P0 |
| 止损 | 10%固定 | 2×ATR(14,4h)，下限8% | 适应BTC波动率结构 | P1 |
| 止盈 | 无 | 1/3在2R + 2/3用3×ATR trailing | 分批锁定利润 | P1 |
| 黑天鹅熔断 | 无 | 1h跌>8%或4h跌>12%减仓50% | BTC黑天鹅是小时级 | P1 |
| 滑点上限 | 无 | 0.5% | 防薄流动性book滑点 | P2 |
| ATR止损启用 | 无 | 默认true | ATR自适应 | P1 |
| equity版本化 | 无 | 加equity_version列 | 历史数据可比性 | P0 |

---

## 执行节奏

| 阶段 | 内容 | 工期 | 累计 |
|------|------|------|------|
| 第1周 | P0（equity+持仓注入+组合敞口） | 3-4天 | 1周 |
| 第2周 | P1（止盈+熔断+ATR自适应） | 5-6天 | 2周 |
| 第3周 | P1.5（最小回测验证P1参数） | 1周 | 3周 |
| 第4-5周 | P2（交易所止损+执行质量+idempotency） | 7-10天 | 5周 |
| 第6-7周 | P3（BTC数据源+市场结构感知） | 2周 | 7周 |
| 第8-11周 | P4（组合层PM，先出架构设计文档） | 3-4周 | 11周 |
| 第12-21周 | P5（完整事件驱动回测框架） | 8-10周 | 21周 |

**前7周（P0-P3）是"从能跑到基本可用"的关键阶段，建议优先完成。**

---

## 验收标准

每个P的验收标准：

| P | 验收标准 |
|---|----------|
| P0 | equity口径正确；PM prompt含持仓；组合敞口>40%被拦截；现有测试全通过+新增P0测试通过 |
| P1 | 止盈触发正常；ATR计算正确；HWM不丢失；熔断1h跌8%触发；测试通过 |
| P1.5 | 6个月历史K线回测完成；ATR参数有数据支撑；输出收益/回撤/胜率/盈亏比 |
| P2 | testnet实测algo order；双触发不重复卖；滑点超限拒绝；fallback正常 |
| P3 | 4个数据源接入；crypto守卫不污染股票；周末/减半上下文注入PM |
| P4 | 组合层rebalance逻辑；波动率平价权重；drift触发；不破坏单标的graph |
| P5 | hash缓存稳定；历史数据回放；缓存命中率>50%；回测vs实盘一致性 |

---

*本文档经3轮agent评审达成一致，评审视角涵盖传统基金经理、BTC实战交易员、技术架构师。*
