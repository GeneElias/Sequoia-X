"""Free annual statements, independent upstream reconciliation, conservative equity model.

Automatic checks are not human review. No API key, analyst target or fabricated input.
The automatic model is deliberately limited to profitable, net-cash nonfinancial firms.
"""

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
import json
from pathlib import Path

import pandas as pd
import requests

from .valuation import number

CIKS = dict(
    zip(
        "MSFT AAPL NVDA GOOGL AMZN META AVGO AMD ORCL CRM ADBE NOW JPM BAC V MA BRK-B UNH JNJ LLY XOM CVX COST WMT PG".split(),
        "789019 320193 1045810 1652044 1018724 1326801 1730168 2488 1341439 1108524 796343 1373715 19617 70858 1403161 1141391 1067983 731766 200406 59478 34088 93410 909832 104169 80424".split(),
    )
)
FINANCIALS = {"JPM", "BAC", "BRK-B", "600036", "601398", "601318", "600030", "601166"}
FIELDS = ("cfo", "capex", "shares", "cash", "debt", "net_income", "common_income", "revenue", "sbc")


class FinancialError(ValueError):
    """Only internally authored, credential-free reasons may be exposed."""


def value(row, *names):
    for name in names:
        if name in row and row[name] is not None and not pd.isna(row[name]):
            return float(number(row[name]))
    raise FinancialError("财报缺少必需字段：" + names[0])


def year_periods(frame):
    result = {}
    for column in frame.columns:
        stamp = pd.Timestamp(column)
        if not pd.isna(stamp):
            result[str(stamp.date())] = frame[column].to_dict()
    return result


def yahoo_statements(symbol, market, as_of):
    import yfinance as yf

    ticker = symbol if market == "US" else symbol + (".SS" if symbol.startswith("6") else ".SZ")
    t = yf.Ticker(ticker)
    cf, inc, bal = [year_periods(x) for x in (t.cash_flow, t.income_stmt, t.balance_sheet)]
    currency = t.fast_info.get("currency")
    if currency != ("USD" if market == "US" else "CNY"):
        raise FinancialError("行情币种不一致")
    rows = []
    for day in sorted(set(cf) & set(inc) & set(bal), reverse=True)[:3]:
        c, i, b = cf[day], inc[day], bal[day]
        # Negative CapEx in Yahoo is a cash outflow, stored below as a positive spend.
        capex = value(c, "Capital Expenditure")
        if capex > 0:
            raise FinancialError("资本开支符号异常")
        row = {
            "date": day,
            "cfo": value(c, "Operating Cash Flow"),
            "capex": -capex,
            "shares": value(b, "Ordinary Shares Number"),
            "cash": value(b, "Cash Cash Equivalents And Short Term Investments") if market == "US" else value(b, "Cash And Cash Equivalents"),
            "debt": value(b, "Current Debt") + value(b, "Long Term Debt") if market == "US" else value(b, "Total Debt"),
            "lease_buffer": value(b, "Capital Lease Obligations") if market == "US" else 0.0,
            "net_income": value(i, "Net Income Including Noncontrolling Interests", "Net Income"),
            "common_income": value(i, "Net Income Common Stockholders", "Net Income"),
            "revenue": value(i, "Total Revenue"),
            "sbc": value(c, "Stock Based Compensation") if market == "US" else 0.0,
        }
        rows.append(row)
    if not rows:
        raise FinancialError("Yahoo没有可用完整年度财报")
    # Annual share counts cannot price a post-report split without conversion.
    events = t.history(
        start=rows[0]["date"],
        end=str(date.fromisoformat(as_of) + timedelta(days=1)),
        auto_adjust=False,
        timeout=12,
    )
    if events.empty or "Stock Splits" not in events or str(events.index[-1].date()) != as_of:
        raise FinancialError("报告期后股本事件覆盖不足")
    if (events["Stock Splits"] != 0).any():
        raise FinancialError("年度财报后发生拆股/送转，需更新股数口径后核验")
    rows[0]["capital_actions_through"] = as_of
    return rows, {
        "upstream": "Yahoo Finance",
        "url": f"https://finance.yahoo.com/quote/{ticker}/financials/",
    }


def sec_fact(facts, tags, end, as_of, annual=False, optional=False):
    for tag in tags:
        units = facts.get(tag, {}).get("units", {})
        entries = units.get("shares" if tag == "CommonStockSharesOutstanding" else "USD", [])
        eligible = []
        for x in entries:
            if (
                x.get("end") != end
                or not x.get("filed")
                or x["filed"] > as_of
                or x.get("form") not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}
            ):
                continue
            if annual:
                if (
                    not x.get("start")
                    or not 350
                    <= (date.fromisoformat(end) - date.fromisoformat(x["start"])).days
                    <= 380
                ):
                    continue
            elif x.get("start"):
                continue
            eligible.append(x)
        if eligible:
            return float(number(max(eligible, key=lambda x: x["filed"])["val"]))
    if optional:
        return None
    raise FinancialError("SEC缺少同报告期字段：" + tags[0])


def sec_statements(symbol, as_of, days, get=requests.get):
    cik = CIKS[symbol].zfill(10)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    r = get(url, headers={"User-Agent": "Sequoia-X research GeneElias/Sequoia-X"}, timeout=15)
    if not r.ok:
        raise FinancialError(f"SEC财报接口HTTP {r.status_code}，未跳过独立核验")
    facts = r.json()["facts"]["us-gaap"]
    rows = []
    evidence = {}
    terms = (
        "debt",
        "borrow",
        "lease",
        "cashandcashequivalents",
        "cashcashequivalentsandshort",
        "paymentstoacquire",
        "commonstockshares",
        "netcashprovidedbyusedinoperat",
        "sharebasedcompensation",
        "profitloss",
        "netincomeloss",
        "revenues",
        "revenuefromcontract",
        "salesrevenuenet",
    )
    for tag, item in facts.items():
        if not any(t in tag.lower() for t in terms):
            continue
        selected = {}
        for unit, entries in item.get("units", {}).items():
            matches = [
                x
                for x in entries
                if x.get("end")
                and any(
                    abs((date.fromisoformat(x["end"]) - date.fromisoformat(d)).days) <= 7
                    for d in days
                )
                and x.get("filed", "9999") <= as_of
            ]
            if matches:
                selected[unit] = matches[-18:]
        if selected:
            evidence[tag] = {"units": selected}
    try:
        for vendor_day in days:
            # Some vendors label 52/53-week fiscal years by month-end. Map only
            # to a unique nearby full-year CFO context, then reconcile every amount.
            ends = set()
            for x in (
                facts.get("NetCashProvidedByUsedInOperatingActivities", {})
                .get("units", {})
                .get("USD", [])
            ):
                if (
                    x.get("filed", "9999") <= as_of
                    and x.get("form") in {"10-K", "10-K/A"}
                    and x.get("start")
                    and abs((date.fromisoformat(x["end"]) - date.fromisoformat(vendor_day)).days)
                    <= 7
                    and 350
                    <= (date.fromisoformat(x["end"]) - date.fromisoformat(x["start"])).days
                    <= 380
                ):
                    ends.add(x["end"])
            if len(ends) != 1:
                raise FinancialError("SEC无法唯一匹配完整财年，未用季度替代")
            day = ends.pop()
            f = lambda tags, annual=False, optional=False: sec_fact(
                facts, tags, day, as_of, annual, optional
            )
            # Canonical funded principal, not Yahoo Total Debt (which can fold
            # in a different lease definition). Match current+noncurrent directly.
            nc = f(['LongTermDebtNoncurrent'],optional=True)
            current = f(['DebtCurrent'],optional=True)
            if current is None:
                cur=f(['LongTermDebtCurrent'],optional=True)
                short=f(['ShortTermBorrowings','ShortTermDebtCurrent','CommercialPaper'],optional=True)
                if cur is None and short is None:
                    raise FinancialError('SEC流动本金债务口径缺失')
                current=(cur or 0)+(short or 0)
            if nc is None:
                total=f(['LongTermDebt'],optional=True)
                if total is None:raise FinancialError('SEC长期本金债务口径缺失')
                cur=f(['LongTermDebtCurrent'],optional=True)
                if cur is None:raise FinancialError('SEC债务总额缺少流动部分分拆')
                nc=total-cur
            total_debt=nc+current
            cash=f(['CashCashEquivalentsAndShortTermInvestments'],optional=True)
            if cash is None:
                cash=f(['CashAndCashEquivalentsAtCarryingValue'])+f(['ShortTermInvestments'])
            finance_lease=f(['FinanceLeaseLiability'],optional=True)
            operating_lease=f(['OperatingLeaseLiability'],optional=True)
            if finance_lease is None and operating_lease is None:
                raise FinancialError('SEC租赁义务口径缺失')
            lease_buffer=(finance_lease or 0)+(operating_lease or 0)
            rows.append(
                {
                    "date": vendor_day,
                    "reported_end": day,
                    "cfo": f(["NetCashProvidedByUsedInOperatingActivities"], True),
                    "capex": f(["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"], True),
                    "shares": f(["CommonStockSharesOutstanding"]),
                    "cash": cash,
                    "lease_buffer": lease_buffer,
                    "debt": total_debt,
                    "net_income": f(["ProfitLoss", "NetIncomeLoss"], True),
                    "common_income": f(
                        ["NetIncomeLossAvailableToCommonStockholdersBasic", "NetIncomeLoss"], True
                    ),
                    "revenue": f(
                        [
                            "RevenueFromContractWithCustomerExcludingAssessedTax",
                            "Revenues",
                            "SalesRevenueNet",
                        ],
                        True,
                    ),
                    "sbc": f(["ShareBasedCompensation"], True),
                }
            )
    except FinancialError as exc:
        exc.evidence = evidence
        raise
    return rows, {"upstream": "SEC", "url": url}


def sina_statements(symbol, as_of, days, get=requests.get):
    code = ("sh" if symbol.startswith("6") else "sz") + symbol
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    sheets = {}
    for name in ("fzb", "lrb", "llb"):
        response = get(
            url,
            params={"paperCode": code, "source": name, "type": "0", "page": "1", "num": "20"},
            timeout=12,
        )
        response.raise_for_status()
        reports = response.json()["result"]["data"]["report_list"]
        sheet = {}
        for stamp, report in reports.items():
            day = pd.Timestamp(stamp).date().isoformat()
            published = str(report.get("publish_date", "")).replace("-", "")[:8]
            if (
                not published
                or published > as_of.replace("-", "")
                or report.get("rCurrency") != "CNY"
            ):
                continue
            # Annual full-year consolidated reports only, never annualize YTD figures.
            if not day.endswith("12-31") or report.get("rType") != "合并期末":
                continue
            sheet[day] = {i["item_title"]: i.get("item_value") for i in report["data"]}
        sheets[name] = sheet
    rows = []

    def nullable(row, name):
        if name not in row:
            raise FinancialError("新浪缺少必需科目：" + name)
        v = row[name]
        return 0.0 if v in (None, "", "--") else float(number(v))

    for day in days:
        try:
            b, i, c = [sheets[n][day] for n in ("fzb", "lrb", "llb")]
        except KeyError:
            raise FinancialError("新浪缺少已公告的同年度合并报表") from None
        # Default A-share universe has RMB1 par shares. Cross-check against Yahoo
        # actual shares; a changed par value fails reconciliation instead of guessing.
        debt = sum(
            nullable(b, n)
            for n in ("短期借款", "长期借款", "应付债券", "一年内到期的非流动负债", "租赁负债")
        )
        rows.append(
            {
                "date": day,
                "cfo": value(c, "经营活动产生的现金流量净额"),
                "capex": value(c, "购建固定资产、无形资产和其他长期资产所支付的现金"),
                "shares": value(b, "实收资本(或股本)", "实收资本（或股本）", "股本"),
                "cash": value(b, "货币资金"),
                "debt": debt,
                "net_income": value(i, "净利润"),
                "common_income": value(i, "归属于母公司所有者的净利润"),
                "revenue": value(i, "营业总收入", "营业收入"),
                "sbc": 0.0,
            }
        )
    return rows, {"upstream": "Sina", "url": url + "?paperCode=" + code}


def reconcile(primary, secondary):
    if len(primary) != 3 or len(secondary) != 3:
        raise FinancialError("需要连续三个完整财年")
    if [r["date"] for r in primary] != [r["date"] for r in secondary]:
        raise FinancialError("两个来源报告期不一致")
    years = [date.fromisoformat(r["date"]) for r in primary]
    if any(not 350 <= (a - b).days <= 380 for a, b in zip(years, years[1:])):
        raise FinancialError("三个财年不连续")
    checks = []
    for a, b in zip(primary, secondary):
        for name in FIELDS:
            av, bv = number(a[name]), number(b[name])
            # Rounding at most one reporting-currency unit, otherwise 1% maximum.
            if abs(av - bv) > max(number(1), abs(av) * number(".01")):
                raise FinancialError(f"双源冲突：{a['date']} {name}差异超过1%")
            checks.append(
                {"date": a["date"], "field": name, "primary": float(av), "secondary": float(bv)}
            )
    return checks


def owner_cash(row):
    # Cap owner earnings by attributable profit. US SBC is deducted as a real cost.
    cfo, capex, sbc = [number(row[k]) for k in ("cfo", "capex", "sbc")]
    ni, common = number(row["net_income"]), number(row["common_income"])
    if ni <= 0 or common <= 0 or common > ni * number("1.01"):
        raise FinancialError("利润为负或归母口径异常")
    cash = (cfo - capex - sbc) * min(number(1), common / ni)
    return min(cash, common)


def verify_record(spec, as_of):
    if spec.get("schema_version") != 2 or spec.get("verification_mode") != "automatic":
        raise FinancialError("自动核验版本无效")
    checked = date.fromisoformat(spec["checked_at"])
    cutoff = date.fromisoformat(as_of)
    if not 0 <= (cutoff - checked).days <= 7:
        raise FinancialError("自动财报核验超过7天或来自未来")
    p, s = spec["primary"], spec["secondary"]
    reconcile(p, s)
    if p[0].get("capital_actions_through") != as_of:
        raise FinancialError("需核查年度财报后至当前交易日的股本事件")
    names = {x["upstream"] for x in spec["sources"]}
    expected = {"Yahoo Finance", "SEC"} if spec["market"] == "US" else {"Yahoo Finance", "Sina"}
    if names != expected:
        raise FinancialError("自动财报需要两个指定独立上游")
    hosts = {"Yahoo Finance": "finance.yahoo.com", "SEC": "data.sec.gov", "Sina": "quotes.sina.cn"}
    if any(
        urlparse(source["url"]).hostname != hosts[source["upstream"]] for source in spec["sources"]
    ):
        raise FinancialError("财报来源网址与实际上游不一致")
    latest = date.fromisoformat(p[0]["date"])
    if not 0 <= (cutoff - latest).days <= 450:
        raise FinancialError("年度财报超过450天或来自未来")
    earnings = []
    for row in p:
        if (
            any(number(row[k]) < 0 for k in ("capex", "cash", "debt", "sbc"))
            or number(row["shares"]) <= 0
            or number(row["revenue"]) <= 0
        ):
            raise FinancialError("财报数值或单位异常")
        other = s[len(earnings)]
        e = min(owner_cash(row), owner_cash(other))
        if e <= 0:
            raise FinancialError("三年中存在负股东现金流，自动模型不适用")
        earnings.append(float(e))
    lease = max(number(rows[0]['lease_buffer']) for rows in (p,s)) if spec['market']=='US' else number(0)
    if lease < 0:raise FinancialError('租赁义务数值无效')
    liquid = min(number(rows[0]['cash']) for rows in (p,s))
    debt = max(number(rows[0]['debt']) for rows in (p,s))
    if liquid < debt + lease:
        raise FinancialError("现金及短期投资不足以覆盖本金债务和租赁缓冲：需人工FCFF或行业模型")
    if number(p[0]["shares"]) / number(p[-1]["shares"]) > number("1.10"):
        raise FinancialError("两年股数增加超过10%，需核查摊薄")
    return earnings


def automatic_assessment(price, currency, spec, as_of):
    from .valuation import dcf

    pending = {"status": "待核验", "verification_mode": "automatic"}
    if spec.get("blocked_reason"):
        return {**pending, "reason": spec["blocked_reason"]}
    try:
        if currency != ("USD" if spec["market"] == "US" else "CNY"):
            raise FinancialError("估值币种不一致")
        if number(price) <= 0:
            raise FinancialError("估值价格必须为正")
        earnings = verify_record(spec, as_of)
        primary = spec["primary"]
        latest = primary[0]
        # Lowest of latest, three-year mean and attributable profit; no cash add-back.
        base_cash = min(earnings[0], sum(earnings) / 3)
        growth = min(
            0.05,
            (earnings[0] / earnings[-1]) ** 0.5 - 1,
            (latest["revenue"] / primary[-1]["revenue"]) ** 0.5 - 1,
            (latest["common_income"] / primary[-1]["common_income"]) ** 0.5 - 1,
        )
        if growth < -0.20:
            raise FinancialError("盈利/现金流快速下降，自动DCF不适用")
        model = {
            "fcff": base_cash,
            "shares": max(r["shares"] for r in primary + spec["secondary"]),
            "net_debt": 0,
            "discount_rate": ".12",
            "terminal_growth": ".02",
            "years": 5,
        }
        growths = {"bear": min(0, growth - 0.05), "base": growth, "bull": min(0.10, growth + 0.05)}
        values = {k: float(dcf(model, number(g))) for k, g in growths.items()}
        margin = 1 - float(number(price)) / values["base"]
        lo, hi = number("-.5"), number(".5")
        reverse = None
        if dcf(model, lo) <= number(price) <= dcf(model, hi):
            for _ in range(70):
                mid = (lo + hi) / 2
                if dcf(model, mid) < number(price):
                    lo = mid
                else:
                    hi = mid
            reverse = float((lo + hi) / 2)
        sensitivity = {
            str(d): float(dcf(model, number(growth), number(".12") + d))
            for d in (number("0"), number(".02"))
        }
        return {
            "status": "有安全边际" if margin >= 0.30 else "等待价格",
            "verification_mode": "automatic",
            "method": "owner_cashflow_dcf",
            "values": values,
            "margin": margin,
            "required_margin": 0.30,
            "watch_price": values["base"] * 0.7,
            "reverse_growth": reverse,
            "rate_sensitivity": sensitivity,
            "financial_as_of": latest["date"],
            "reviewed_at": spec["checked_at"],
            "sources": spec["sources"],
            "growth": growths,
            "cashflow_base": base_cash,
            "discrepancies": [],
            "reason": "双源数值核对通过；三年正现金流、正利润及年度净现金筛选通过。未自动判断护城河或管理层质量。",
            "assumptions_reason": "净现金企业股东现金流代理：min(经营现金流−资本开支−美股股权激励，经归母占比折算,归母利润)；基数取最新与三年均值较低者，增长取现金流/收入/利润两年复合增速最低且≤5%；折现12%、永续2%、安全边际30%。美股核对流动及长期本金债务；现金含短期投资，另按两源租赁义务较大值留缓冲。不加回现金、不重复扣净债务。A股股权激励无独立调整。",
            "review_note": "自动财报数值与规则核验，未经人工审阅；银行、保险、券商及净负债企业仍需专用人工模型。",
        }
    except FinancialError as exc:
        return {**pending, "reason": str(exc)}
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return {**pending, "reason": "自动财报记录不完整或模型验证失败"}


class Fundamentals:
    def __init__(self, path: Path):
        self.path = path
        self.records = json.loads(path.read_text()) if path.exists() else {}

    def refresh(self, market, symbols, as_of, force=False):
        for symbol in symbols:
            key = f"{market}:{symbol}"
            old = self.records.get(key, {})
            age = (
                date.fromisoformat(as_of) - date.fromisoformat(old.get("checked_at", "2000-01-01"))
            ).days
            if (
                not force
                and old.get("schema_version") == 2
                and age == 0
                and (
                    old.get("blocked_reason")
                    or old.get("primary", [{}])[0].get("capital_actions_through") == as_of
                )
            ):
                continue
            spec = {
                "schema_version": 2,
                "verification_mode": "automatic",
                "market": market,
                "checked_at": as_of,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                if symbol in FINANCIALS:
                    raise FinancialError("金融企业需银行/保险/券商专用模型，暂停自动估值")
                rows, source = yahoo_statements(symbol, market, as_of)
                rows = [r for r in rows if r["date"] <= as_of][:3]
                if len(rows) < 3:
                    raise FinancialError("Yahoo不足三个完整年度财报")
                spec.update(primary=rows, sources=[source])
                days = [r["date"] for r in rows]
                other, source2 = (
                    sec_statements(symbol, as_of, days)
                    if market == "US"
                    else sina_statements(symbol, as_of, days)
                )
                spec.update(primary=rows, secondary=other, sources=[source, source2])
                spec["input_checks"] = reconcile(rows, other)
                verify_record(spec, as_of)
            except FinancialError as exc:
                spec["blocked_reason"] = str(exc)
                if hasattr(exc, "evidence"):
                    spec["secondary_evidence"] = exc.evidence
            except Exception as exc:
                spec["blocked_reason"] = f"财报获取失败（{type(exc).__name__}），未以空值补零"
            self.records[key] = spec
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.records, ensure_ascii=False, allow_nan=False))
        temp.replace(self.path)
        return {f"{market}:{s}": self.records[f"{market}:{s}"] for s in symbols}
