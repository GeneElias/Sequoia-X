"""Research report assembly and Feishu-compatible card rendering."""

from datetime import datetime, timezone
import hashlib
import json

import pandas as pd

from .market import MarketData, latest_session
from .valuation import assess


DEFAULT_UNIVERSE = {
    "US": "MSFT AAPL NVDA GOOGL AMZN META AVGO AMD ORCL CRM ADBE NOW JPM BAC V MA BRK-B UNH JNJ LLY XOM CVX COST WMT PG".split(),
    "A": "600519 000858 000568 600036 601398 601318 600030 600900 601088 600028 601857 600276 000333 000651 600887 002415 002475 300750 300760 601012 600309 601899 600406 601166 002594".split(),
}

NAMES = dict(zip(DEFAULT_UNIVERSE["US"], [
    "微软", "苹果", "英伟达", "Alphabet", "亚马逊", "Meta", "博通", "AMD", "甲骨文", "Salesforce",
    "Adobe", "ServiceNow", "摩根大通", "美国银行", "Visa", "Mastercard", "伯克希尔B", "联合健康",
    "强生", "礼来", "埃克森美孚", "雪佛龙", "Costco", "沃尔玛", "宝洁",
]))
NAMES.update(dict(zip(DEFAULT_UNIVERSE["A"], [
    "贵州茅台", "五粮液", "泸州老窖", "招商银行", "工商银行", "中国平安", "中信证券", "长江电力",
    "中国神华", "中国石化", "中国石油", "恒瑞医药", "美的集团", "格力电器", "伊利股份", "海康威视",
    "立讯精密", "宁德时代", "迈瑞医疗", "隆基绿能", "万华化学", "紫金矿业", "国电南瑞", "兴业银行", "比亚迪",
])))


def indicators(frame: pd.DataFrame, market: str) -> dict:
    """Common strategies; no A-share limit-up/down rules are applied to US stocks."""
    close, volume = frame.close, frame.volume
    last, previous = frame.iloc[-1], frame.iloc[-2]
    high20 = frame.high.iloc[-21:-1].max()
    breakout = bool(last.close > high20 and last.close > last.open and last.close > previous.close)
    avg_volume = volume.iloc[-21:-1].mean()
    volume_ratio = float(last.volume / avg_volume) if avg_volume > 0 else None
    ma5, ma20 = close.rolling(5).mean(), close.rolling(20).mean()
    # Mirror existing MaVolumeStrategy's mean (which includes the current bar).
    cross = bool(ma5.iloc[-2] < ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]
                 and last.volume > volume.rolling(20).mean().iloc[-1] * 1.5)
    tail40, tail10 = frame.tail(40), frame.tail(10)
    flag = bool(tail40.high.max() / tail40.low.min() > 1.6
                and tail10.high.max() / tail10.low.min() < 1.15
                and tail10.low.min() >= tail40.high.max() * .8
                and avg_volume > 0 and last.volume < avg_volume * .6)
    amount = float(last.amount) if "amount" in frame else None
    liquid = amount is not None and amount > 100_000_000 if market == "A" else True
    signals = []
    if breakout and liquid:
        signals.append("20日阳线突破" if market == "US" else "海龟突破（成交额过亿）")
    if cross:
        signals.append("均线金叉放量")
    if flag:
        signals.append("高位缩量旗形（等待突破）")
    high120 = float(frame.high.tail(120).max())
    return {"return120": float(last.close / close.iloc[-121] - 1),
            "breakout_pct": float(last.close / high20 - 1), "high20_adjusted": float(high20),
            "near_high": bool(last.close >= high120 * .90), "volume_ratio": volume_ratio,
            "signals": signals, "confirmed": bool((breakout and liquid) or cross),
            "raw_price": float(last.raw_close), "amount": amount,
            "score": int(breakout and liquid) * 3 + int(cross) * 2 + int(flag)}


def build_report(universe: dict, provider: MarketData, valuations: dict,
                 now: datetime | None = None, top: int = 10) -> dict:
    now = now or datetime.now(timezone.utc)
    report = {"schema_version": 1, "generated_at": now.isoformat(), "demo": False,
              "scope": "用户指定/内置研究观察池，非全市场最优或历史指数成分股",
              "markets": [], "valuation_framework": "AI Berkshire 方法参考；自实现计算，不执行旧预设"}
    for market, symbols in universe.items():
        cutoff = latest_session(market, now)
        group = {"market": market, "as_of": cutoff, "pool": symbols, "pool_size": len(symbols),
                 "items": [], "errors": [], "complete": False}
        for symbol in symbols:
            try:
                h = provider.history(symbol, market, cutoff)
                item = {"symbol": symbol, "name": NAMES.get(symbol, symbol), "market": market, "date": str(h.frame.index[-1]),
                        "source": h.source, "upstream": h.upstream, "currency": h.currency,
                        "adjustment": h.adjustment, "warnings": h.warnings}
                if h.adjustment != "consistent":
                    group["errors"].append({"symbol": symbol, "reason": "备源复权未核实，未参与排名"})
                    continue
                item.update(indicators(h.frame, market))
                item["valuation"] = assess(item["raw_price"], h.currency,
                                            valuations.get(f"{market}:{symbol}"), cutoff)
                group["items"].append(item)
            except Exception as exc:
                # No vendor exception text: it could include a credential-bearing URL.
                group["errors"].append({"symbol": symbol, "reason": f"日线获取或验证失败（{type(exc).__name__}）"})
        items = group["items"]
        group["complete"] = len(items) == len(symbols) and len(items) >= 2
        if group["complete"]:
            ranks = pd.Series([x["return120"] for x in items]).rank(pct=True) * 100
        for i, item in enumerate(items):
            item["rps"] = float(ranks.iloc[i]) if group["complete"] else None
            if item["rps"] is not None and item["rps"] >= 90 and item["near_high"]:
                item["signals"].append("RPS强势接近120日高位（非严格突破）")
                item["score"] += 2
            v = item["valuation"]["status"]
            if v == "否决":
                item["status"] = "剔除：质量否决"
            elif not group["complete"]:
                item["status"] = "待数据：股票池不完整"
            elif not item["confirmed"]:
                item["status"] = "观察：等待量价确认"
            elif v == "待核验":
                item["status"] = "待核验：估值未完成"
            elif v == "等待价格":
                item["status"] = "等待价格：安全边际不足"
            else:
                item["status"] = "买入研究候选（非交易指令）"
        items.sort(key=lambda x: (x["status"].startswith("买入研究"), x["score"],
                                   x["rps"] or 0, x["symbol"]), reverse=True)
        group["candidate_count"] = sum(x["status"].startswith("买入研究") for x in items)
        group["shown"] = items[:top]
        report["markets"].append(group)
    return report


def pct(value) -> str:
    return "待核验" if value is None else f"{value * 100:.1f}%"


def item_markdown(item: dict) -> str:
    symbol = item["symbol"]
    link = f"https://finance.yahoo.com/quote/{symbol}/" if item["market"] == "US" else f"https://quote.eastmoney.com/{symbol}.html"
    rps = "暂不计算（池不完整）" if item["rps"] is None else f"{item['rps']:.1f}（池内）"
    volume = "待核验" if item["volume_ratio"] is None else f"{item['volume_ratio']:.2f}倍"
    lines = [f"**[{item.get('name', symbol)} · {symbol}]({link})｜{item['status']}**",
             f"行情：{item['date']}｜{item['raw_price']:.2f} {item['currency']}",
             f"RPS：{rps}｜120日回报：{pct(item['return120'])}",
             f"距前20日高点：{pct(item['breakout_pct'])}｜量比：{volume}",
             "信号：" + ("；".join(item["signals"]) or "当前未触发策略确认"),
             "理由：" + ("趋势信号已出现，还需结合估值与风险。" if item["confirmed"] else "量价尚未确认，保留观察。")]
    v = item["valuation"]
    if "values" in v:
        values = v["values"]
        lines += [f"估值：悲观 {values['bear']:.2f}／基准 {values['base']:.2f}／乐观 {values['bull']:.2f} {item['currency']}（现值）",
                  f"基准安全边际：{pct(v['margin'])}｜假设阈值：{pct(v['required_margin'])}",
                  f"模型观察价 ≤ {v['watch_price']:.2f}｜市场隐含FCFF增长：{pct(v['reverse_growth'])}",
                  f"财报：{v['financial_as_of']}｜输入复核：{v['reviewed_at']}（用户标记）",
                  f"质量审查：{v['reason']}"]
    else:
        lines.append(f"估值：**{v['status']}**，{v['reason']}")
    lines += ["风险：突破失败、现金流不及假设或估值上修依据消失；悲观情景不是最大损失。",
              f"来源：{item['source']} → {item['upstream']}"]
    return "\n".join(lines)


def market_markdown(group: dict) -> str:
    title = "🇺🇸 美股" if group["market"] == "US" else "🇨🇳 A股"
    lines = [f"## {title}｜完整收盘日 {group['as_of']}",
             f"观察池 {group['pool_size']} 支｜有效 {len(group['items'])} 支｜取数/质量异常 {len(group['errors'])} 支",
             f"买入研究候选 {group['candidate_count']} 支；不足10支不凑数。",
             "股票池：" + ", ".join(group["pool"])]
    if not group["complete"]:
        lines.append("⚠️ 股票池不完整或不足2支：不输出RPS和买入候选；已取到数据仅供观察。")
    lines += [item_markdown(x) for x in group["shown"]]
    if group["errors"]:
        lines.append("异常：" + "；".join(f"{x['symbol']} {x['reason']}" for x in group["errors"]))
    return "\n\n".join(lines)


def render_markdown(report: dict) -> str:
    return "\n\n".join([
        "# 📊 Sequoia-X｜每日选股与估值复核",
        "🧪 演示数据，非实时结论" if report["demo"] else "真实取数报告；未核验的估值不填充，不作为买入指令。",
        report["scope"],
        "量比使用此前20日均量；均线策略忠于原实现，放量基准包含当日。RPS仅在同市场完整观察池内计算。",
        *(market_markdown(g) for g in report["markets"]),
        "估值方法：FCFF DCF＋三情景现值＋反向DCF＋折现率上调2个百分点压力测试。"
        "质量与来源由输入材料复核，不使用历史报告预设冒充最新估值。完整假设与来源见 report.json。",
        "免费接口可能延迟或失败；日期为最后完整交易日，不是盘中实时行情。"])


def render_cards(report: dict) -> list[dict]:
    """Split long reports into byte-bounded cards; never put localhost links in buttons."""
    cards = []
    for group in report["markets"]:
        sections = [f"完整收盘日：{group['as_of']}\n观察池 {group['pool_size']}｜有效 {len(group['items'])}｜异常 {len(group['errors'])}\n"
                    f"买入研究候选 {group['candidate_count']}；不足10支不凑数。\n"
                    "自定义观察池，非全市场排名。估值缺失=待核验。",
                    *(item_markdown(x) for x in group["shown"])]
        if not group["complete"]:
            sections.insert(1, "⚠️ 股票池不完整或不足2支，RPS与买入候选暂不输出。")
        if group["errors"]:
            sections.append("取数异常：" + ", ".join(x["symbol"] for x in group["errors"]))
        sections.append("量比=此前20日均量；估值基于显式输入与复核状态。行情可能延迟，非交易指令。")
        pages, page = [], []
        for section in sections:
            # Lark custom bot message limit is 20 KB. Reserve room for JSON overhead.
            if sum(len(s.encode()) for s in page) + len(section.encode()) > 14000 and page:
                pages.append(page)
                page = []
            if len(section.encode()) > 14000:
                raise ValueError("单支股票的卡片内容过长")
            page.append(section)
        if page:
            pages.append(page)
        for index, page in enumerate(pages, 1):
            payload = {"msg_type": "interactive", "card": {
                "config": {"wide_screen_mode": True},
                "header": {"template": "orange" if group["errors"] or report["demo"] else "blue",
                           "title": {"tag": "plain_text", "content":
                                     f"{'[演示] ' if report['demo'] else ''}Sequoia-X｜{group['market']}｜{group['as_of']} ({index}/{len(pages)})"}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": s}} for s in page]}}
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 19000:
                raise ValueError("飞书消息超过安全字节上限")
            cards.append(payload)
    return cards


def content_id(card: dict) -> str:
    return hashlib.sha256(json.dumps(card, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
