"""Reproducible valuation; never create financial inputs or growth forecasts.

Reviewed assumptions must be supplied explicitly. This is inspired by AI Berkshire's
financial-data / investment-research methods, not execution of its presets or reports.
"""

from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import urlparse


def number(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("估值输入必须为有限数")
    return result


def dcf(spec: dict, growth: Decimal, rate: Decimal | None = None) -> Decimal:
    """FCFF -> enterprise value -> equity value, all amounts in the same currency."""
    fcf, shares = number(spec["fcff"]), number(spec["shares"])
    r = number(spec["discount_rate"]) if rate is None else rate
    g = number(spec["terminal_growth"])
    years = int(spec.get("years", 5))
    if fcf <= 0 or shares <= 0 or not 1 <= years <= 20 or r - g < Decimal("0.05"):
        raise ValueError("FCFF/股数/预测期无效或 r-g 少于5个百分点")
    if not Decimal("-0.5") <= growth <= Decimal("0.5") or g < 0 or r > Decimal("0.5"):
        raise ValueError("增长率或折现率超出可支持范围")
    with localcontext() as ctx:
        ctx.prec = 28
        value = Decimal(0)
        for year in range(1, years + 1):
            cash = fcf * (1 + growth) ** year
            value += cash / (1 + r) ** year
        value += cash * (1 + g) / (r - g) / (1 + r) ** years
        return (value - number(spec["net_debt"])) / shares


def assess(price: float, currency: str, spec: dict | None, as_of: str) -> dict:
    pending = {"status": "待核验", "reason": "未提供已复核财报与估值假设，不生成目标价"}
    if not spec:
        return pending
    try:
        if spec.get("reviewed") is not True:
            return {**pending, "reason": "估值材料未标记为人工复核完成"}
        if spec.get("currency") != currency or spec.get("method") != "fcff_dcf":
            raise ValueError("仅支持同币种 FCFF DCF；银行保险等不适用")
        stamp = date.fromisoformat(spec["reviewed_at"])
        cutoff = date.fromisoformat(as_of)
        if not 0 <= (cutoff - stamp).days <= 90:
            raise ValueError("复核日期为未来或超过90天")
        financial_at = date.fromisoformat(spec["financial_as_of"])
        if not 0 <= (cutoff - financial_at).days <= 450 or financial_at > stamp:
            raise ValueError("财报日期无效或过旧")
        if not spec.get("assumptions_reason") or not spec.get("quality_reason"):
            raise ValueError("缺少假设或质量审查理由")
        sources = spec["sources"]
        # Count actual upstreams, not SDK names or two Yahoo wrappers.
        providers = set()
        for source in sources:
            domain = urlparse(source["url"]).hostname or ""
            if not domain or not source.get("upstream"):
                raise ValueError("缺少来源上游及URL")
            canonical = source["upstream"].strip().lower()
            for upstream in ("yahoo.com", "sec.gov", "eastmoney.com"):
                if domain == upstream or domain.endswith("." + upstream):
                    canonical = upstream
            providers.add(canonical)
        if len(providers) < 2 or not spec.get("source_checks"):
            raise ValueError("需两种上游来源与逐项数值核对")
        discrepancies = []
        checked_fields = set()
        for item in spec["source_checks"]:
            checked_fields.add(item["field"])
            a, b = number(item["primary"]), number(item["secondary"])
            if item["field"] in {"fcff", "shares", "net_debt"} and a != number(spec[item["field"]]):
                raise ValueError("模型输入与主来源数值不一致")
            if a == 0:
                difference = Decimal(0) if b == 0 else Decimal(1)
            else:
                difference = abs(a - b) / abs(a)
            if difference > Decimal("0.01"):
                discrepancies.append(item["field"])
            if difference > Decimal("0.05"):
                raise ValueError("关键数据差异超过5%，必须回查原始财报")
        if not {"fcff", "shares", "net_debt"} <= checked_fields:
            raise ValueError("FCFF、股数与净债务都必须核对")
        if spec.get("quality_status") == "fail":
            return {"status": "否决", "reason": spec["quality_reason"]}
        if spec.get("quality_status") != "pass":
            return {**pending, "reason": "质量审查尚未通过"}
        growths = spec["growth"]
        values = {name: dcf(spec, number(growths[name])) for name in ("bear", "base", "bull")}
        if not 0 < values["bear"] <= values["base"] <= values["bull"]:
            raise ValueError("情景现值非正或次序不合理")
        margin = 1 - number(price) / values["base"]
        required = number(spec.get("required_margin", "0.2"))
        if not 0 < required < 1:
            raise ValueError("安全边际阈值必须在0与1之间")
        # Reverse DCF: growth needed to justify the market price under the same model.
        lo, hi = Decimal("-0.5"), Decimal("0.5")
        reverse = None
        if dcf(spec, lo) <= number(price) <= dcf(spec, hi):
            for _ in range(80):
                mid = (lo + hi) / 2
                if dcf(spec, mid) < number(price):
                    lo = mid
                else:
                    hi = mid
            reverse = float((lo + hi) / 2)
        sensitivity = {str(delta): float(dcf(spec, number(growths["base"]),
                                           number(spec["discount_rate"]) + delta))
                       for delta in (Decimal(0), Decimal("0.02"))}
        return {"status": "有安全边际" if margin >= required else "等待价格",
                "reason": spec["quality_reason"], "values": {k: float(v) for k, v in values.items()},
                "margin": float(margin), "required_margin": float(required),
                "watch_price": float(values["base"] * (1 - required)),
                "reverse_growth": reverse, "rate_sensitivity": sensitivity,
                "financial_as_of": spec["financial_as_of"], "reviewed_at": str(stamp),
                "sources": sources, "discrepancies": discrepancies,
                "assumptions_reason": spec["assumptions_reason"],
                "review_note": "基于用户标记已复核的输入；系统未替代原文人工审阅"}
    except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation, ZeroDivisionError):
        return {**pending, "reason": "估值输入不完整、过期、来源不足或模型约束未通过，需复核"}
