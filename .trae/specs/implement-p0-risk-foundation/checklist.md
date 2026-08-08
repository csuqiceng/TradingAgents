# P0 风控地基 Checklist

## Equity 口径修复
- [ ] `_quote_equity` 方法计算总权益（quote + base×price）
- [ ] 价格获取失败时降级为 quote-only 并记录 warning
- [ ] `get_account_snapshot` 返回 `equity_total` 而非 `equity_quote`
- [ ] 单测：{"USDT":2000,"BTC":0.05}+price=60000 → 5000
- [ ] 单测：价格失败 → 回退 quote-only

## Equity 版本化
- [ ] `cycles` 表新增 `equity_version` 列（走 _MIGRATIONS）
- [ ] 新记录写入 equity_version=2
- [ ] 旧记录保留 equity_version=1
- [ ] 迁移幂等（重复执行不报错）

## 配置项
- [ ] `crypto_max_portfolio_exposure=0.4` 配置项 + env 映射
- [ ] `crypto_min_cash_ratio=0.25` 配置项 + env 映射
- [ ] `crypto_max_position` 默认值改为 0.15
- [ ] legacy 环境变量 fallback 不破坏

## 组合总敞口上限
- [ ] `portfolio_exposure_check` 函数实现
- [ ] `min_cash_check` 函数实现
- [ ] broker `_submit` 加组合敞口检查（BUY 时）
- [ ] broker `_submit` 加现金比例检查（BUY 时）
- [ ] SELL 不受组合敞口和现金比例限制
- [ ] 单测：35% 放行 BUY
- [ ] 单测：42% 拒绝 BUY + "portfolio_exposure_breach"
- [ ] 单测：现金 20% 拒绝 BUY + "min_cash_breach"
- [ ] 单测：SELL 不受限

## PM 持仓注入
- [ ] loop.py run_once 注入 portfolio_snapshot 到 graph state
- [ ] PM prompt 包含持仓快照（positions + 总敞口 + 占比 + 浮盈）
- [ ] PM prompt 包含仓位软约束提示（接近上限/已超上限）
- [ ] 集成测：prompt 文本含持仓快照
- [ ] 集成测：接近上限时含警告文案

## daily_report 兼容性
- [ ] daily_report 不依赖 equity_quote 字段名（或已更新）
- [ ] daily_report PnL 计算不受 equity 口径变更影响

## 回归测试
- [ ] `pytest tests/test_runner_state.py tests/test_crypto_execution.py -v` 全通过
- [ ] 全量 `pytest tests/ -v` 无回归
- [ ] 代理逻辑（exchange.proxies）未被触碰
- [ ] 无新增过度工程（未加无关配置项/抽象）

## 硬约束验证
- [ ] 现货约束保留（defaultType: 'spot' 不变）
- [ ] 代理逻辑（exchange.proxies）未被修改
- [ ] 冷却机制未被破坏
- [ ] 反思循环未被破坏
