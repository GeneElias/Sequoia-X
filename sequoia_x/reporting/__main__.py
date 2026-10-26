"""python -m sequoia_x.reporting --help"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from dotenv import load_dotenv

from .delivery import DeliveryError, send_cards, validate_webhook, with_dashboard
from .market import MarketData
from .report import DEFAULT_UNIVERSE, build_report, render_cards, render_markdown


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="零预算两市研究报告；默认只生成本地预览，不发送")
    parser.add_argument("--market", choices=["both", "US", "A"], default="both")
    parser.add_argument("--us-symbols", nargs="+", help="自定义美股观察池，如 MSFT AAPL NVDA")
    parser.add_argument("--a-symbols", nargs="+", help="自定义A股观察池，如 600519 000333")
    parser.add_argument("--valuations", type=Path, help="经复核的估值输入 JSON；未提供时全部估值待核验")
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--refresh", action="store_true", help="刷新行情缓存（不反复重试限流）")
    parser.add_argument("--top", type=int, default=10, choices=range(1, 11), metavar="1..10")
    parser.add_argument("--send", action="store_true", help="显式发送到本地配置的飞书机器人")
    parser.add_argument("--force-resend", action="store_true", help="确认群消息后强制重发，包括结果不明的消息")
    parser.add_argument("--send-existing", type=Path, help="发送已生成的 cards.json，不重新取行情；须同时指定 --send")
    args = parser.parse_args(argv)
    load_dotenv()
    try:
        if args.force_resend and not args.send:
            parser.error("--force-resend 必须与 --send 一起使用")
        if args.send_existing and not args.send:
            parser.error("--send-existing 必须与 --send 一起使用")
        webhook = os.environ.get("FEISHU_REPORT_WEBHOOK_URL") or os.environ.get("FEISHU_WEBHOOK_URL", "")
        if args.send:
            validate_webhook(webhook)
        state = args.output / "_state"
        if args.send_existing:
            cards = json.loads(args.send_existing.read_text())
            if not isinstance(cards, list) or not cards or any(
                not isinstance(c, dict) or c.get("msg_type") != "interactive" or "card" not in c for c in cards
            ):
                raise ValueError("cards.json 必须是非空飞书交互卡片列表")
            if any(len(json.dumps(c, ensure_ascii=False).encode()) > 19000 for c in cards):
                raise ValueError("消息过长")
        else:
            universe = {}
            for market, supplied in [("US", args.us_symbols), ("A", args.a_symbols)]:
                if args.market not in ("both", market):
                    continue
                symbols = list(dict.fromkeys(s.upper() for s in (supplied or DEFAULT_UNIVERSE[market])))
                pattern = r"[A-Z][A-Z0-9.-]{0,14}" if market == "US" else r"[036]\d{5}"
                if not all(re.fullmatch(pattern, s) for s in symbols) or len(symbols) > 100:
                    raise ValueError("代码格式错误或观察池超过100支")
                universe[market] = symbols
            valuations = json.loads(args.valuations.read_text()) if args.valuations else {}
            if not isinstance(valuations, dict):
                raise ValueError("估值输入必须是对象")
            provider = MarketData(state / "cache", refresh=args.refresh)
            report = build_report(universe, provider, valuations, top=args.top)
            cards = render_cards(report)
            directory = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            directory.mkdir(parents=True, exist_ok=False)
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
            (directory / "report.md").write_text(render_markdown(report))
            (directory / "cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2, allow_nan=False))
            print(f"已生成报告：{directory / 'report.md'}")
            print(f"飞书卡片：{directory / 'cards.json'}（{len(cards)}张）")
            for group in report["markets"]:
                print(f"{group['market']} {group['as_of']}: 有效 {len(group['items'])}/{group['pool_size']}，"
                      f"买入研究候选 {group['candidate_count']}，异常 {len(group['errors'])}")
        if args.send:
            cards = with_dashboard(cards, os.environ.get("FEISHU_DASHBOARD_URL"))
            result = send_cards(cards, webhook, state / "delivery.sqlite",
                                os.environ.get("FEISHU_REPORT_SECRET"), args.force_resend)
            print(f"飞书发送成功 {result['sent']} 张，已发送去重跳过 {result['skipped']} 张")
        elif not args.send_existing:
            print("预览模式：未发送消息；确认内容后可使用 --send-existing <cards.json> --send")
        return 0
    except DeliveryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"报告失败（{type(exc).__name__}），未输出可能含敏感值的异常正文", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
