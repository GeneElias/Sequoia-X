"""Self-contained dashboard export. Read-only, no network and no secrets embedded."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


ASSETS = Path(__file__).parent / "dashboard_assets"


def select(obj: dict, fields: str) -> dict:
    return {k: obj.get(k) for k in fields.split()}


def empty_dataset(public: bool = False) -> dict:
    data = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "audience": "public" if public else "local", "markets": {}, "scheduler": {},
            "basis": "仅模拟；价格收益，不含股息税费；CNY/USD分别核算；新增投入不是利润。"}
    for market, currency in [("A", "CNY"), ("US", "USD")]:
        data["markets"][market] = {"currency": currency, "as_of": None, "valid": 0, "pool_size": 0,
                                    "candidate_count": 0, "observations": [], "history": [],
                                    "orders": [], "fills": [], "fill_count": 0, "positions": [], "weekly": [],
                                    "complete": False}
    if public:
        data["basis"] = "公开页面尚未发布真实研究与账本数据；不生成虚构股票、持仓或收益。"
    return data


def dataset(root: Path) -> dict:
    data = empty_dataset()
    # Whitelist each field. Never export raw SQLite, assumptions, source files, URLs or .env.
    for path in sorted(root.glob("paper/daily/*/report.json")):
        report = json.loads(path.read_text())
        if report.get("demo") is not False:
            continue
        for group in report.get("markets", []):
            m = data["markets"].get(group["market"])
            if m is None or (m["as_of"] and group["as_of"] < m["as_of"]):
                continue
            m.update(as_of=group["as_of"], valid=len(group["items"]), pool_size=group["pool_size"],
                     candidate_count=group["candidate_count"], complete=group["complete"])
            m["observations"] = []
            for item in group["items"]:
                row = select(item, "symbol name date raw_price currency rps return120 volume_ratio breakout_pct status signals confirmed source upstream")
                row["valuation"] = select(item.get("valuation", {}), "status margin watch_price financial_as_of reviewed_at")
                m["observations"].append(row)
        paper = path.with_name("paper.json")
        if paper.exists():
            state = json.loads(paper.read_text())
            market = state.get("market")
            if market in data["markets"]:
                data["markets"][market]["delivery"] = state.get("delivery", {}).get("status", "unknown")
    book = root / "_state" / "paper.sqlite"
    if book.exists():
        with sqlite3.connect(book.resolve().as_uri() + "?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            policy = c.execute("SELECT spec FROM policy WHERE id=1").fetchone()
            data["policy"] = json.loads(policy[0]) if policy else {}
            for market, m in data["markets"].items():
                for snap in c.execute("SELECT * FROM snapshots WHERE market=? ORDER BY day", (market,)):
                    equity = float(snap["equity"]) if snap["complete"] and snap["equity"] is not None else None
                    capital = float(snap["contributed"])
                    m["history"].append({"day": snap["day"], "equity": equity, "contributed": capital,
                                          "pnl": equity - capital if equity is not None else None,
                                          "return": (equity - capital) / capital if equity is not None and capital else None,
                                          "complete": bool(snap["complete"])})
                latest = c.execute("SELECT * FROM snapshots WHERE market=? ORDER BY day DESC LIMIT 1", (market,)).fetchone()
                if latest:
                    m["positions"] = [select(x, "symbol quantity price value cost pnl status")
                                      for x in json.loads(latest["details"])]
                m["orders"] = [dict(x) for x in c.execute(
                    "SELECT symbol,signal_date,execution_date,state FROM orders WHERE market=? ORDER BY signal_date DESC,symbol LIMIT 500", (market,))]
                m["fills"] = [dict(x) for x in c.execute(
                    "SELECT symbol,day,quantity,price,fee,cost FROM fills WHERE market=? ORDER BY day DESC,symbol LIMIT 500", (market,))]
                m["fill_count"] = c.execute("SELECT COUNT(*) FROM fills WHERE market=?", (market,)).fetchone()[0]
    for path in sorted(root.glob("paper/weekly/*/review.json"))[-52:]:
        weekly = json.loads(path.read_text())
        for group in weekly.get("markets", []):
            if group["market"] not in data["markets"]:
                continue
            row = select(group, "complete as_of weekly_pnl weekly_return total_pnl total_return fill_count new_capital equity")
            row.update(week_start=weekly["week_start"], week_end=weekly["week_end"])
            data["markets"][group["market"]]["weekly"].append(row)
    heartbeat = root / "_state" / "scheduler-status.json"
    if heartbeat.exists():
        data["scheduler"] = select(json.loads(heartbeat.read_text()), "heartbeat timezone lifetime")
    return data


def export_dashboard(root: Path, output: Path, public: bool = False) -> Path:
    # Public pages never read a private root, including one containing valid reports.
    data = empty_dataset(public=True) if public else dataset(root)
    # Keep JSON inside a non-executable script element without allowing </script> breakout.
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    escaped = (encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    template = (ASSETS / "index.html").read_text()
    html = (template.replace("__CSS__", (ASSETS / "dashboard.css").read_text())
            .replace("__JS__", (ASSETS / "dashboard.js").read_text()).replace("__DATA__", escaped))
    output.mkdir(parents=True, exist_ok=True)
    # Local/private artifacts are ignored. Export is not deployment or permission to publish.
    (output / "dashboard.json").write_text(encoded)
    temp = output / "index.html.tmp"
    temp.write_text(html)
    temp.replace(output / "index.html")
    (output / ".nojekyll").touch()
    return output / "index.html"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成离线交互看板，默认私密本地数据；不自动公开发布")
    parser.add_argument("--root", type=Path, default=Path("reports"))
    parser.add_argument("--output", type=Path, default=Path("reports/dashboard"))
    parser.add_argument("--public-empty", action="store_true", help="生成无数据公开页面；不读取真实报告/账本，不生成示例")
    args = parser.parse_args(argv)
    path = export_dashboard(args.root, args.output, args.public_empty)
    print(f"看板已生成：{path}（{'公开空状态' if args.public_empty else '本地私密数据，未公开发布'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
