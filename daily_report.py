#!/usr/bin/env python3
"""TradingAgents-BTC 每日交易报告。

数据源：
- OKX 模拟盘成交明细（fetch_my_trades）—— 交易记录
- 本地 SQLite runner_state.db（cycles.decision_md / orders）—— 为什么这么交易
- OKX 余额快照 —— 权益与营收占比

输出：Markdown 文本报告，推送到飞书 Webhook（也可 dry-run 打印）。

用法：
  python daily_report.py             # 报告"今天"（北京时间）
  python daily_report.py 2026-08-07  # 报告指定日期
  python daily_report.py --dry-run   # 只打印到 stdout，不发飞书
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.crypto_broker import CryptoBroker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("daily_report")

# 北京时间 = UTC+8
TZ_CST = dt.timezone(dt.timedelta(hours=8))


def load_broker() -> CryptoBroker:
    cfg = DEFAULT_CONFIG
    return CryptoBroker(
        exchange_id=cfg.get("crypto_exchange", "okx"),
        api_key=cfg.get("crypto_api_key"),
        secret=cfg.get("crypto_secret"),
        passphrase=cfg.get("crypto_passphrase"),
        https_proxy=cfg.get("crypto_https_proxy"),
        testnet=cfg.get("execution_mode", "paper") == "paper",
        quote_budget=cfg.get("crypto_quote_budget", 1000.0),
        max_position_fraction=cfg.get("crypto_max_position", 0.2),
        cooldown_seconds=cfg.get("crypto_cooldown_seconds", 14400.0),
    )


def local_db() -> sqlite3.Connection:
    db_path = DEFAULT_CONFIG.get("runner_db_path") or os.path.join(
        DEFAULT_CONFIG.get("data_cache_dir", os.path.expanduser("~/.tradingagents/cache")),
        "runner_state.db",
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_trades(broker: CryptoBroker, since_ms: int, until_ms: int) -> list[dict]:
    """拉取 [since_ms, until_ms) 区间的全部成交（不限币种，分页）。"""
    all_trades: list[dict] = []
    after = None
    while True:
        params: dict = {"since": since_ms, "limit": 100}
        if after:
            params["after"] = after
        batch = broker.exchange.fetch_my_trades(None, **params)
        if not batch:
            break
        all_trades.extend(batch)
        if len(batch) < 100:
            break
        after = batch[-1]["id"]
    # ccxt 可能返回跨区间的数据，按时间过滤
    filtered = [
        t for t in all_trades
        if t.get("timestamp") is not None and since_ms <= t["timestamp"] < until_ms
    ]
    filtered.sort(key=lambda t: t["timestamp"])
    return filtered


def summarize_pnl(trades: list[dict]) -> dict:
    """按币种 FIFO 简化配对，估算当日已实现盈亏与占比。

    返回:
      {
        "by_symbol": {sym: {"buy_qty","buy_cost","sell_qty","sell_income",
                            "fee","pnl","pnl_pct_share","n"}},
        "by_side": {"buy": {"amount","fee"}, "sell": {"amount","fee"}},
        "total_fee", "total_pnl", "n_trades"
      }
    """
    by_symbol: dict[str, dict] = defaultdict(lambda: {
        "buy_qty": 0.0, "buy_cost": 0.0, "sell_qty": 0.0, "sell_income": 0.0,
        "fee": 0.0, "pnl": 0.0, "n": 0,
    })
    by_side = {"buy": {"amount": 0.0, "fee": 0.0}, "sell": {"amount": 0.0, "fee": 0.0}}
    total_fee = 0.0

    for t in trades:
        sym = t["symbol"]
        side = t["side"]
        amount = float(t.get("amount") or 0)
        price = float(t.get("price") or 0)
        fee = 0.0
        f = t.get("fee")
        if isinstance(f, dict) and f.get("cost"):
            fee = abs(float(f["cost"]))
        elif isinstance(f, float):
            fee = abs(f)
        notional = amount * price

        row = by_symbol[sym]
        row["n"] += 1
        row["fee"] += fee
        total_fee += fee
        by_side[side]["amount"] += notional
        by_side[side]["fee"] += fee

        if side == "buy":
            row["buy_qty"] += amount
            row["buy_cost"] += notional
        else:
            row["sell_qty"] += amount
            row["sell_income"] += notional

    # 简化盈亏：卖出收入 - 买入成本（仅按当日配对；超出部分视为动用历史持仓，
    # 成本按当日买入均价估算）
    total_pnl = 0.0
    for sym, row in by_symbol.items():
        if row["buy_qty"] > 0:
            avg_cost = row["buy_cost"] / row["buy_qty"]
        else:
            avg_cost = None
        pnl = row["sell_income"] - row["buy_cost"]
        # 若卖出量 > 买入量，超出部分按买入均价估算成本
        excess = row["sell_qty"] - row["buy_qty"]
        if excess > 0 and avg_cost:
            est_cost = 0.0
            # 用卖出明细逐笔补算：成本 = 当日卖出的总数量对应的均价成本
            # 这里简化：超出部分按均价成本计入
            for t in trades:
                if t["symbol"] == sym and t["side"] == "sell":
                    est_cost += float(t.get("amount") or 0) * avg_cost
            # est_cost 是全部卖出量的估算成本，pnl 修正为：
            pnl = row["sell_income"] - est_cost
        row["pnl"] = pnl
        total_pnl += pnl

    # 占比
    for row in by_symbol.values():
        row["pnl_pct_share"] = (row["pnl"] / total_pnl * 100) if total_pnl else 0.0

    return {
        "by_symbol": dict(by_symbol),
        "by_side": by_side,
        "total_fee": total_fee,
        "total_pnl": total_pnl,
        "n_trades": len(trades),
    }


def load_decisions(db: sqlite3.Connection, day_start_ts: float, day_end_ts: float) -> list[dict]:
    """拉取当天本地的分析轮次（决策理由）。"""
    rows = db.execute(
        "SELECT id, ticker, rating, order_status, equity_before, equity_after, "
        "       decision_md, started_at, ended_at, error "
        "FROM cycles WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
        (day_start_ts, day_end_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def load_orders(db: sqlite3.Connection, day_start_ts: float, day_end_ts: float) -> list[dict]:
    rows = db.execute(
        "SELECT cycle_id, symbol, action, status, rating, price, amount, reason, ts "
        "FROM orders WHERE ts >= ? AND ts < ? ORDER BY ts",
        (day_start_ts, day_end_ts),
    ).fetchall()
    return [dict(r) for r in rows]


def extract_decision_summary(decision_md: str | None) -> str:
    """从完整决策 Markdown 中提取关键摘要（Rating + Executive Summary）。"""
    if not decision_md:
        return "（无决策记录）"
    lines = []
    in_exec = False
    for ln in decision_md.splitlines():
        s = ln.strip()
        if s.startswith("**Rating**"):
            lines.append(s)
        elif s.startswith("**Executive Summary**"):
            in_exec = True
            continue
        elif in_exec:
            if s.startswith("**") and s != "**Executive Summary**":
                in_exec = False
            elif s:
                lines.append(s)
        if len(lines) >= 4:  # Rating + 3 句摘要足够
            break
    if not lines:
        # 退化为截断全文
        return decision_md[:300] + ("..." if len(decision_md) > 300 else "")
    return "\n".join(lines)


def build_report(report_date: dt.date, trades: list[dict], decisions: list[dict],
                 orders: list[dict], pnl: dict, equity_start: float | None,
                 equity_end: float | None) -> str:
    """拼装 Markdown 报告文本。"""
    L: list[str] = []
    L.append(f"📊 TradingAgents-BTC 每日交易报告")
    L.append(f"📅 日期：{report_date.isoformat()}（北京时间）")
    L.append("")

    # ---- 1. 交易记录 ----
    L.append("━━━ ① 今日交易记录 ━━━")
    if not trades:
        L.append("今日无成交。")
    else:
        L.append(f"成交 {len(trades)} 笔（买 {sum(1 for t in trades if t['side']=='buy')} / "
                 f"卖 {sum(1 for t in trades if t['side']=='sell')}）")
        L.append("")
        for t in trades[:30]:  # 最多列 30 笔，避免超长
            ts = dt.datetime.fromtimestamp(t["timestamp"] / 1000, tz=TZ_CST).strftime("%H:%M:%S")
            fee = t.get("fee") or {}
            fee_str = f"{abs(float(fee['cost'])):.4f} {fee.get('currency','')}" if isinstance(fee, dict) and fee.get("cost") else "-"
            side_mark = "🟢买" if t["side"] == "buy" else "🔴卖"
            L.append(f"`{ts}` {side_mark} {t['symbol']}  {t['amount']} @ {t['price']}  "
                     f"额≈{float(t['amount'])*float(t['price']):,.1f}  手续费 {fee_str}")
        if len(trades) > 30:
            L.append(f"...（共 {len(trades)} 笔，其余省略）")
    L.append("")

    # ---- 2. 营收与占比 ----
    L.append("━━━ ② 营收与占比 ━━━")
    L.append(f"当日成交总额：买入 {pnl['by_side']['buy']['amount']:,.2f} USDT / "
             f"卖出 {pnl['by_side']['sell']['amount']:,.2f} USDT")
    L.append(f"手续费合计：{pnl['total_fee']:.4f} USDT")
    L.append(f"估算已实现盈亏：**{pnl['total_pnl']:+,.2f} USDT**")
    if pnl["by_symbol"]:
        L.append("")
        L.append("按币种占比：")
        for sym, row in sorted(pnl["by_symbol"].items(), key=lambda x: -abs(x[1]["pnl"])):
            share = f"{row['pnl_pct_share']:+.1f}%" if row["pnl_pct_share"] else "0.0%"
            L.append(f"  • {sym}：{row['pnl']:+,.2f} USDT（占比 {share}，{row['n']} 笔）")
    if equity_start is not None and equity_end is not None:
        eq_delta = equity_end - equity_start
        L.append(f"账户权益：{equity_start:,.2f} → {equity_end:,.2f} USDT（{eq_delta:+,.2f}）")
    L.append("")

    # ---- 3. 为什么这么交易 ----
    L.append("━━━ ③ 为什么这么交易 ━━━")
    if not decisions and not orders:
        L.append("（当日无分析轮次记录）")
    else:
        for d in decisions:
            ticker = d["ticker"]
            rating = d.get("rating") or "?"
            order_status = d.get("order_status") or "?"
            ts = dt.datetime.fromtimestamp(d["started_at"], tz=TZ_CST).strftime("%H:%M")
            L.append(f"▎{ts} {ticker} 分析轮 #{d['id']} → 评级 **{rating}** / 订单 {order_status}")
            if d.get("error"):
                L.append(f"  ⚠️ 该轮出错：{d['error'].splitlines()[0][:120]}")
            summary = extract_decision_summary(d.get("decision_md"))
            for ln in summary.splitlines():
                L.append(f"  {ln}")
            L.append("")
        # 补充订单原因（若与分析轮不重合）
        for o in orders:
            if o.get("reason") and o.get("status") not in ("none",):
                ts = dt.datetime.fromtimestamp(o["ts"], tz=TZ_CST).strftime("%H:%M")
                L.append(f"  • {ts} {o['symbol']} {o['action']}: {o['reason']}")
    L.append("")
    L.append("—— 由 TradingAgents-BTC 自动生成 ——")
    return "\n".join(L)


def send_feishu(text: str, webhook: str) -> bool:
    """推送文本消息到飞书 Webhook（国内直连，无需代理）。"""
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(webhook, json=payload, timeout=15)
        ok = resp.status_code == 200 and resp.json().get("code") == 0
        if not ok:
            logger.error("飞书推送失败: HTTP %s body=%s", resp.status_code, resp.text[:300])
        return ok
    except Exception as exc:
        logger.error("飞书推送异常: %s", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="TradingAgents-BTC 每日交易报告")
    parser.add_argument("date", nargs="?", help="报告日期 YYYY-MM-DD（默认今天，北京时间）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发飞书")
    args = parser.parse_args()

    if args.date:
        report_date = dt.date.fromisoformat(args.date)
    else:
        report_date = dt.datetime.now(TZ_CST).date()

    # 北京时间日界 → UTC 时间戳
    day_start_cst = dt.datetime(report_date.year, report_date.month, report_date.day, tzinfo=TZ_CST)
    day_end_cst = day_start_cst + dt.timedelta(days=1)
    start_ms = int(day_start_cst.timestamp() * 1000)
    end_ms = int(day_end_cst.timestamp() * 1000)
    start_ts = start_ms / 1000
    end_ts = end_ms / 1000

    logger.info("报告日期 %s（CST），区间 %s ~ %s",
                report_date, day_start_cst.isoformat(), day_end_cst.isoformat())

    # 拉数据
    broker = load_broker()
    trades = fetch_trades(broker, start_ms, end_ms)
    logger.info("OKX 成交 %d 笔", len(trades))

    db = local_db()
    decisions = load_decisions(db, start_ts, end_ts)
    orders = load_orders(db, start_ts, end_ts)
    logger.info("本地分析轮 %d 个 / 订单记录 %d 条", len(decisions), len(orders))

    # 权益（优先用本地记录，其次实时快照）
    equity_start = None
    equity_end = None
    if decisions:
        equity_start = decisions[0].get("equity_before")
        equity_end = decisions[-1].get("equity_after")
        if equity_start is None:
            for d in decisions:
                if d.get("equity_before") is not None:
                    equity_start = d["equity_before"]
                    break
        if equity_end is None:
            for d in reversed(decisions):
                if d.get("equity_after") is not None:
                    equity_end = d["equity_after"]
                    break
    if equity_end is None:
        try:
            snap = broker.get_account_snapshot()
            if "error" not in snap:
                equity_end = snap.get("equity_quote")
        except Exception as exc:
            logger.warning("实时权益获取失败: %s", exc)

    pnl = summarize_pnl(trades)
    report = build_report(report_date, trades, decisions, orders, pnl, equity_start, equity_end)

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60 + "\n")

    if args.dry_run:
        logger.info("dry-run 模式，不发送飞书")
        return 0

    webhook = DEFAULT_CONFIG.get("feishu_webhook") or os.getenv("TRADINGAGENTS_FEISHU_WEBHOOK")
    if not webhook:
        logger.error("未配置飞书 Webhook（TRADINGAGENTS_FEISHU_WEBHOOK），跳过发送")
        return 2
    ok = send_feishu(report, webhook)
    logger.info("飞书推送 %s", "成功 ✅" if ok else "失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
