"""Daily paper orders, weekly reviews and a restartable in-environment scheduler.

python -m sequoia_x.reporting.simulation daily
python -m sequoia_x.reporting.simulation weekly
python -m sequoia_x.reporting.simulation daemon --send
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv, dotenv_values

from .delivery import send_cards, validate_webhook, with_dashboard
from .dashboard import export_dashboard
from .market import MarketData, latest_session
from .paper import PaperBook, weekly_cards, weekly_markdown
from .report import DEFAULT_UNIVERSE, build_report, render_cards, render_markdown


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))
    temp.replace(path)


class Simulation:
    def __init__(self, root: Path, valuations: Path | None = None, send: bool = False,
                 slippage_bps: str = "5", fee_per_order: str = "0", checkpoint=None):
        self.root = root
        marker = root / "_state" / "github-migration.json"
        if marker.exists() and not os.environ.get("GITHUB_ACTIONS") == "true":
            if json.loads(marker.read_text()).get("phase") == "github_active":
                raise RuntimeError("后台已由GitHub接管，禁止写入过期云端账本；预览请使用reporting入口")
        self.valuations_path = valuations
        self.send = send
        self.checkpoint = checkpoint
        self.book = PaperBook(root / "_state" / "paper.sqlite", slippage_bps, fee_per_order)
        self.provider = MarketData(root / "_state" / "cache")

    def deliver(self, cards: list[dict]) -> dict:
        if not self.send:
            return {"status": "preview_only"}
        # Read local configuration fresh: load_dotenv(override=False) would keep stale
        # values previously loaded into the long-running daemon's environment.
        local = dotenv_values(".env")
        def setting(name):
            return local[name] if name in local else os.environ.get(name)
        webhook = setting("FEISHU_REPORT_WEBHOOK_URL") or setting("FEISHU_WEBHOOK_URL") or ""
        if not webhook:
            return {"status": "blocked_missing_webhook"}
        validate_webhook(webhook)
        cards = with_dashboard(cards, setting("FEISHU_DASHBOARD_URL"))
        return {"status": "attempted", **send_cards(
            cards, webhook, self.root / "_state" / "delivery.sqlite", setting("FEISHU_REPORT_SECRET"),
            checkpoint=self.checkpoint)}

    def daily(self, market: str = "both", now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        valuations = (json.loads(self.valuations_path.read_text())
                      if self.valuations_path and self.valuations_path.exists() else {})
        markets = ["A", "US"] if market == "both" else [market]
        output = []
        for code in markets:
            as_of = latest_session(code, now)
            summary = self.book.settle(code, as_of, self.provider)
            report = build_report({code: DEFAULT_UNIVERSE[code]}, self.provider, valuations, now=now)
            summary["new_orders"] = self.book.register(report, now=now)
            cards = render_cards(report)
            with self.book.connect() as conn:
                signals = conn.execute("SELECT symbol,execution_date,state FROM orders WHERE market=? AND signal_date=? ORDER BY symbol",
                                       (code, as_of)).fetchall()
            note = (f"仅模拟｜当日合格信号登记 {len(signals)} 笔，每只100股，下个交易日开盘模拟成交。\n"
                    f"累计订单状态：{json.dumps(summary['orders'], ensure_ascii=False)}（新增信号另见账本）\n"
                    f"累计虚拟投入 {summary['contributed']} {'CNY' if code=='A' else 'USD'}；"
                    f"滑点{self.book.slippage*10000}bp、手续费{self.book.fee}/笔；价格收益不含股息税费；同日信号去重，持有不卖。")
            if signals:
                note += "\n" + "；".join(f"{r['symbol']} 100股，预计{r['execution_date']}（{r['state']}）" for r in signals)
            cards[-1]["card"]["elements"].append({"tag": "div", "text": {"tag": "lark_md", "content": note}})
            directory = self.root / "paper" / "daily" / f"{as_of}-{code}"
            directory.mkdir(parents=True, exist_ok=True)
            write_json(directory / "report.json", report)
            write_json(directory / "paper.json", summary)
            write_json(directory / "cards.json", cards)
            (directory / "report.md").write_text(render_markdown(report) + "\n\n## 模拟记账\n\n" + note)
            summary["delivery"] = self.deliver(cards)
            write_json(directory / "paper.json", summary)
            output.append({"market": code, "as_of": as_of, "new_orders": summary["new_orders"],
                           "report": str(directory / "report.md"), "delivery": summary["delivery"]})
        export_dashboard(self.root, self.root / "dashboard")
        return {"daily": output}

    def weekly(self, week_end: str | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if week_end is None:
            local = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
            week_end = str(local - timedelta(days=(local.weekday() - 4) % 7))
        # Refresh holdings to the current complete session, never register new signals here.
        for market in ("A", "US"):
            self.book.settle(market, latest_session(market, now), self.provider)
        report = self.book.weekly(week_end)
        report["cost_policy"] = {"slippage_bps": str(self.book.slippage * 10000),
                                 "fee_per_order": str(self.book.fee)}
        cards = weekly_cards(report)
        directory = self.root / "paper" / "weekly" / week_end
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "review.json", report)
        write_json(directory / "cards.json", cards)
        (directory / "review.md").write_text(weekly_markdown(report))
        result = {"weekly": str(directory / "review.md"), "delivery": self.deliver(cards)}
        export_dashboard(self.root, self.root / "dashboard")
        return result


def due_jobs(now: datetime) -> list[tuple[str, str]]:
    """All times Shanghai: US Tue-Sat 07:30, A Mon-Fri 17:00, weekly Sat 09:00."""
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    clock = (local.hour, local.minute)
    day = str(local.date())
    jobs = []
    if 1 <= local.weekday() <= 5 and clock >= (7, 30):
        jobs.append((day + ":daily_US", "US"))
    if local.weekday() <= 4 and clock >= (17, 0):
        jobs.append((day + ":daily_A", "A"))
    if local.weekday() == 5 and clock >= (9, 0):
        jobs.append((day + ":weekly", "weekly"))
    return jobs


def run_due(simulation: Simulation, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    state = simulation.root / "_state"
    with sqlite3.connect(state / "schedule.sqlite", timeout=30) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, state TEXT, started TEXT, outcome TEXT)")
        results = []
        for identity, kind in due_jobs(now):
            conn.execute("BEGIN IMMEDIATE")
            record = conn.execute("SELECT state,started FROM jobs WHERE id=?", (identity,)).fetchone()
            if record and (record[0] in {"done", "failed"} or (record[0] == "running" and
                    now - datetime.fromisoformat(record[1]) < timedelta(hours=6))):
                conn.commit()
                continue
            conn.execute("INSERT OR REPLACE INTO jobs VALUES (?, 'running', ?, '')", (identity, now.isoformat()))
            conn.commit()
            try:
                result = simulation.weekly(now=now) if kind == "weekly" else simulation.daily(kind, now=now)
                conn.execute("UPDATE jobs SET state='done',outcome=? WHERE id=?", (json.dumps(result), identity))
                results.append({"job": identity, **result})
            except Exception as exc:
                conn.execute("UPDATE jobs SET state='failed',outcome=? WHERE id=?", (type(exc).__name__, identity))
                results.append({"job": identity, "error": type(exc).__name__})
            conn.commit()
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A股+美股每只100股模拟记账；绝不连接实盘账户")
    parser.add_argument("command", choices=["daily", "weekly", "run-due", "daemon"])
    parser.add_argument("--market", choices=["both", "A", "US"], default="both")
    parser.add_argument("--root", type=Path, default=Path("reports"))
    parser.add_argument("--valuations", type=Path, default=Path("valuations.local.json"),
                        help="默认读取本地估值文件；文件不存在时估值待核验，不登记买单")
    parser.add_argument("--week-end", help="周复盘截止日 YYYY-MM-DD；默认最近周五")
    parser.add_argument("--send", action="store_true", help="配置Webhook后尝试发送，否则记录缺失并保留本地报告")
    parser.add_argument("--slippage-bps", default="5")
    parser.add_argument("--fee-per-order", default="0")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    load_dotenv()
    try:
        sim = Simulation(args.root, args.valuations, args.send, args.slippage_bps, args.fee_per_order)
        if args.command == "daily":
            result = sim.daily(args.market)
        elif args.command == "weekly":
            result = sim.weekly(args.week_end)
        elif args.command == "run-due":
            result = run_due(sim)
        else:
            if args.poll_seconds < 15:
                raise ValueError("定时轮询不得少于15秒")
            while True:
                now = datetime.now(timezone.utc)
                result = run_due(sim, now)
                write_json(args.root / "_state" / "scheduler-status.json", {
                    "heartbeat": now.isoformat(), "pid": os.getpid(),
                    "timezone": "Asia/Shanghai", "results": result,
                    "lifetime": "仅在当前云环境保持运行时有效，休眠或停止后不会自动唤醒"})
                if result:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                time.sleep(args.poll_seconds)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"模拟任务失败（{type(exc).__name__}），敏感异常正文已隐藏", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
