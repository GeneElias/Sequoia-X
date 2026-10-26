"""Forward-only, cash-injected paper purchases. Never connects to a broker.

100 shares per daily recommended symbol, next-session open, hold without selling.
Price-only P&L, splits applied, dividends/taxes excluded and explicitly disclosed.
Missed execution sessions expire rather than being hindsight-filled.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pandas as pd

from .market import latest_session
from .valuation import number


def sessions(market: str, start: str, end: str) -> list[str]:
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS" if market == "US" else "XSHG", start=start, end=end)
    return [str(s.date()) for s in cal.sessions]


def next_session(market: str, day: str) -> str:
    start = date.fromisoformat(day) + timedelta(days=1)
    return sessions(market, str(start), str(start + timedelta(days=30)))[0]


class PaperBook:
    def __init__(self, path: Path, slippage_bps: str = "5", fee_per_order: str = "0"):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.slippage = number(slippage_bps) / 10000
        self.fee = number(fee_per_order)
        if not 0 <= self.slippage <= Decimal("0.05") or self.fee < 0:
            raise ValueError("滑点或每笔费用无效")
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), spec TEXT);
                CREATE TABLE IF NOT EXISTS orders (
                    market TEXT, symbol TEXT, signal_date TEXT, execution_date TEXT,
                    state TEXT, reason TEXT, PRIMARY KEY(market,symbol,signal_date));
                CREATE TABLE IF NOT EXISTS fills (
                    market TEXT, symbol TEXT, signal_date TEXT, day TEXT,
                    quantity TEXT, price TEXT, fee TEXT, cost TEXT, source TEXT,
                    PRIMARY KEY(market,symbol,signal_date));
                CREATE TABLE IF NOT EXISTS positions (
                    market TEXT, symbol TEXT, quantity TEXT, cost TEXT, action_date TEXT,
                    PRIMARY KEY(market,symbol));
                CREATE TABLE IF NOT EXISTS snapshots (
                    market TEXT, day TEXT, equity TEXT, contributed TEXT, fees TEXT,
                    details TEXT, complete INTEGER, PRIMARY KEY(market,day));
            """)
            spec = json.dumps({"quantity": 100, "slippage": str(self.slippage), "fee": str(self.fee),
                               "dividends": "excluded", "selling": "hold"}, sort_keys=True)
            old = c.execute("SELECT spec FROM policy WHERE id=1").fetchone()
            if old and old[0] != spec:
                raise ValueError("账本已有不同费用策略，请使用独立账本，不混算收益")
            c.execute("INSERT OR IGNORE INTO policy VALUES (1,?)", (spec,))

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def register(self, report: dict, now: datetime | None = None) -> int:
        """Only today's completed-session, fully reviewed recommendations are orders."""
        now = now or datetime.now(timezone.utc)
        if report.get("demo") is not False:
            raise ValueError("演示或未标明性质的报告不能注册模拟订单")
        added = 0
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for group in report["markets"]:
                market, day = group["market"], group["as_of"]
                if day != latest_session(market, now):
                    raise ValueError("过期或未来信号不能追溯建仓")
                if not group["complete"]:
                    continue
                already = c.execute("SELECT COUNT(*) FROM orders WHERE market=? AND signal_date=?",
                                    (market, day)).fetchone()[0]
                for item in group["shown"]:
                    if not (item["status"].startswith("买入研究候选") and item["confirmed"]
                            and item["valuation"]["status"] == "有安全边际" and item["date"] == day):
                        continue
                    if already >= 10:
                        break
                    result = c.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?,?, 'pending','')",
                                       (market, item["symbol"], day, next_session(market, day)))
                    if result.rowcount:
                        added += 1
                        already += 1
        return added

    def settle(self, market: str, as_of: str, provider) -> dict:
        """Settle at latest complete session. No late backfills of missed orders."""
        with self.connect() as c:
            last = c.execute("SELECT MAX(day) FROM snapshots WHERE market=?", (market,)).fetchone()[0]
            if last and as_of < last:
                raise ValueError("不能向过去回写模拟账户")
            orders = c.execute("SELECT * FROM orders WHERE market=? AND state='pending' AND execution_date<=?",
                               (market, as_of)).fetchall()
            symbols = {r["symbol"] for r in orders}
            symbols.update(r[0] for r in c.execute("SELECT symbol FROM positions WHERE market=?", (market,)))
        quotes, errors = {}, []
        for symbol in sorted(symbols):
            try:
                h = provider.trading_history(symbol, market, as_of)
                if h.frame.index[-1] != as_of:
                    raise ValueError("模拟价格过期")
                quotes[symbol] = h
            except Exception as exc:
                errors.append(f"{symbol}: {type(exc).__name__}")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            latest_written = c.execute("SELECT MAX(day) FROM snapshots WHERE market=?", (market,)).fetchone()[0]
            if latest_written and as_of < latest_written:
                raise ValueError("并发任务已写入更晚日期，不能回写过去")
            # Splits affect prior holdings before today's new 100-share purchase.
            for position in c.execute("SELECT * FROM positions WHERE market=?", (market,)).fetchall():
                h = quotes.get(position["symbol"])
                if h is None:
                    continue
                if position["action_date"] < h.frame.index[0]:
                    errors.append(position["symbol"] + ": corporate-action history incomplete")
                    quotes.pop(position["symbol"])
                    continue
                qty = number(position["quantity"])
                splits = h.frame.loc[h.frame.index > position["action_date"], "stock_splits"]
                for ratio in splits:
                    if number(ratio) > 0:
                        qty *= number(ratio)
                c.execute("UPDATE positions SET quantity=?,action_date=? WHERE market=? AND symbol=?",
                          (str(qty), as_of, market, position["symbol"]))
            for order in orders:
                key = (market, order["symbol"], order["signal_date"])
                # Recheck after obtaining lock: a concurrent worker may have filled it.
                state = c.execute("SELECT state FROM orders WHERE market=? AND symbol=? AND signal_date=?", key).fetchone()[0]
                if state != "pending":
                    continue
                if order["execution_date"] < as_of:
                    c.execute("UPDATE orders SET state='expired',reason='错过成交日，不追溯补单' WHERE market=? AND symbol=? AND signal_date=?", key)
                    continue
                h = quotes.get(order["symbol"])
                if h is None:
                    continue
                row = h.frame.loc[as_of]
                price = number(row["raw_open"])
                if price <= 0 or number(row["volume"]) <= 0:
                    c.execute("UPDATE orders SET state='unfilled',reason='无有效开盘成交量' WHERE market=? AND symbol=? AND signal_date=?", key)
                    continue
                prev = h.frame.iloc[-2]
                # Conservative A-share filter: rising one-price session is not assumed fillable.
                locked_up = (market == "A" and row["raw_open"] == row["raw_high"] == row["raw_low"] == row["raw_close"]
                             and row["raw_close"] > prev["raw_close"])
                if locked_up:
                    c.execute("UPDATE orders SET state='unfilled',reason='一字上涨日，保守假设无法买入' WHERE market=? AND symbol=? AND signal_date=?", key)
                    continue
                if market == "A" and order["symbol"].startswith("688") and not c.execute(
                        "SELECT 1 FROM positions WHERE market=? AND symbol=?", key[:2]).fetchone():
                    c.execute("UPDATE orders SET state='unfilled',reason='科创板首笔最低200股，本计划固定100股不成交' WHERE market=? AND symbol=? AND signal_date=?", key)
                    continue
                fill_price = price * (1 + self.slippage)
                cost = fill_price * 100 + self.fee
                c.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                          (*key, as_of, "100", str(fill_price), str(self.fee), str(cost), h.upstream))
                position = c.execute("SELECT * FROM positions WHERE market=? AND symbol=?", key[:2]).fetchone()
                qty = Decimal(100) + (number(position["quantity"]) if position else 0)
                total_cost = cost + (number(position["cost"]) if position else 0)
                c.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?)",
                          (market, order["symbol"], str(qty), str(total_cost), as_of))
                c.execute("UPDATE orders SET state='filled' WHERE market=? AND symbol=? AND signal_date=?", key)
            details, equity = [], Decimal(0)
            all_positions = c.execute("SELECT * FROM positions WHERE market=?", (market,)).fetchall()
            complete = True
            for position in all_positions:
                h = quotes.get(position["symbol"])
                if h is None:
                    complete = False
                    details.append({"symbol": position["symbol"], "status": "价格待核验"})
                    continue
                mark = number(h.frame.loc[as_of, "raw_close"])
                value = number(position["quantity"]) * mark
                equity += value
                details.append({"symbol": position["symbol"], "quantity": position["quantity"],
                                "price": str(mark), "value": str(value), "cost": position["cost"],
                                "pnl": str(value - number(position["cost"]))})
            fills = c.execute("SELECT cost,fee FROM fills WHERE market=?", (market,)).fetchall()
            contributed = sum((number(r["cost"]) for r in fills), Decimal(0))
            fees = sum((number(r["fee"]) for r in fills), Decimal(0))
            c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
                      (market, as_of, str(equity) if complete else None, str(contributed), str(fees),
                       json.dumps(details, ensure_ascii=False), int(complete)))
            states = {r[0]: r[1] for r in c.execute("SELECT state,COUNT(*) FROM orders WHERE market=? GROUP BY state", (market,))}
        return {"market": market, "as_of": as_of, "complete": complete, "equity": str(equity) if complete else None,
                "contributed": str(contributed), "fees": str(fees), "orders": states, "errors": errors}

    def weekly(self, week_end: str) -> dict:
        end = date.fromisoformat(week_end)
        monday = end - timedelta(days=end.weekday())
        previous_monday = monday - timedelta(days=7)
        result = {"week_start": str(monday), "week_end": str(end), "markets": [],
                  "basis": "价格收益；持有不卖；默认每笔注入所需虚拟资金；未计股息和税费，不混算汇率"}
        with self.connect() as c:
            for market, currency in [("A", "CNY"), ("US", "USD")]:
                days = sessions(market, str(monday), str(end))
                prior = sessions(market, str(previous_monday), str(monday - timedelta(days=1)))
                day = days[-1] if days else (prior[-1] if prior else None)
                baseline_day = prior[-1] if prior else None
                latest = c.execute("SELECT * FROM snapshots WHERE market=? AND day=?", (market, day)).fetchone()
                baseline = c.execute("SELECT * FROM snapshots WHERE market=? AND day=?", (market, baseline_day)).fetchone()
                item = {"market": market, "currency": currency, "as_of": day, "complete": False}
                fills = c.execute("SELECT * FROM fills WHERE market=? AND day>=? AND day<=?", (market, str(monday), str(end))).fetchall()
                earlier = c.execute("SELECT COUNT(*) FROM fills WHERE market=? AND day<?", (market, str(monday))).fetchone()[0]
                if latest and latest["complete"] and ((baseline and baseline["complete"]) or not earlier):
                    start_value = number(baseline["equity"]) if baseline and baseline["complete"] else Decimal(0)
                    cash_in = sum((number(f["cost"]) for f in fills), Decimal(0))
                    value, capital = number(latest["equity"]), number(latest["contributed"])
                    pnl = value - start_value - cash_in
                    denominator = start_value + cash_in
                    item.update(complete=True, start_equity=str(start_value), new_capital=str(cash_in),
                                equity=str(value), weekly_pnl=str(pnl), total_pnl=str(value - capital),
                                contributed=str(capital), fill_count=len(fills),
                                weekly_return=float(pnl / denominator) if denominator else None,
                                total_return=float((value - capital) / capital) if capital else None,
                                fees=latest["fees"], positions=json.loads(latest["details"]))
                else:
                    item["reason"] = "缺少完整期初/期末估值，不能把缺失价格视为零或已实现收益"
                item["pending_orders"] = c.execute("SELECT COUNT(*) FROM orders WHERE market=? AND state='pending'", (market,)).fetchone()[0]
                result["markets"].append(item)
        return result


def weekly_markdown(report: dict) -> str:
    lines = [f"# 📒 两市模拟组合周复盘｜{report['week_start']} 至 {report['week_end']}",
             "仅模拟，不连接交易账户。每日每只合格推荐100股，下个交易日开盘模拟成交，重复推荐可加仓。",
             report["basis"], "周收益率=周盈亏/(期初市值+本周新增投入)，非年化、非时间加权收益率。"]
    if report.get("cost_policy"):
        p = report["cost_policy"]
        lines.append(f"模拟买入滑点 {p['slippage_bps']}bp，每笔手续费 {p['fee_per_order']}（各市场本币）。费用已计入投入成本。")
    for g in report["markets"]:
        lines.append(f"## {'🇨🇳 A股' if g['market']=='A' else '🇺🇸 美股'}｜{g['currency']}｜{g['as_of']}")
        if not g["complete"]:
            lines.append("待核验：" + g["reason"])
            continue
        percent = "无投入，不计算收益率" if g["weekly_return"] is None else f"{g['weekly_return']*100:.2f}%"
        lines += [f"本周成交 {g['fill_count']} 笔｜待成交 {g['pending_orders']} 笔",
                  f"期初市值 {g['start_equity']}｜新增虚拟投入 {g['new_capital']}｜期末市值 {g['equity']}",
                  f"**本周价格盈亏 {g['weekly_pnl']} {g['currency']}｜收益率 {percent}**",
                  f"累计投入 {g['contributed']}｜累计价格盈亏 {g['total_pnl']}｜累计模拟手续费 {g['fees']}"]
        lines.extend(f"{p['symbol']}：{p['quantity']} 股｜市值 {p['value']}｜价格盈亏 {p['pnl']}" for p in g["positions"])
    lines.append("无合格推荐则不买；不将观察项或估值待核验项偷换为推荐。未设置卖出策略，收益为持仓浮动价格收益。")
    return "\n\n".join(lines)


def weekly_cards(report: dict) -> list[dict]:
    text = weekly_markdown(report)
    # Each paragraph is a separate element; split before 14KB to respect 20KB bot limit.
    pages, page, size = [], [], 0
    for section in text.split("\n\n"):
        n = len(section.encode())
        if size + n > 14000 and page:
            pages.append(page)
            page, size = [], 0
        page.append(section)
        size += n
    if page:
        pages.append(page)
    return [{"msg_type": "interactive", "card": {"header": {"template": "blue", "title": {
        "tag": "plain_text", "content": f"Sequoia-X｜两市模拟周复盘｜{report['week_end']} ({i}/{len(pages)})"}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": s}} for s in page]}}
        for i, page in enumerate(pages, 1)]
