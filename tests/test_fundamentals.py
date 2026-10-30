"""Adversarial checks for automatic statements; no network or fake human approval."""

from copy import deepcopy
from unittest.mock import Mock

import pandas as pd
import pytest

from sequoia_x.reporting.fundamentals import (
    FinancialError,
    Fundamentals,
    automatic_assessment,
    reconcile,
    sec_fact,
    verify_record,
)
from sequoia_x.reporting.valuation import assess


def automatic(market="US"):
    primary = [
        {
            "date": f"{year}-12-31",
            "cfo": 1300.0,
            "capex": 200.0,
            "shares": 100.0,
            "cash": 2000.0,
            "debt": 100.0,
            "lease_buffer": 0.0,
            "net_income": 1200.0,
            "common_income": 1100.0,
            "revenue": 5000.0,
            "sbc": 100.0 if market == "US" else 0.0,
        }
        for year in (2025, 2024, 2023)
    ]
    primary[0]["capital_actions_through"] = "2026-10-30"
    return {
        "schema_version": 2,
        "verification_mode": "automatic",
        "market": market,
        "checked_at": "2026-10-29",
        "primary": primary,
        "secondary": deepcopy(primary),
        "sources": [
            {"upstream": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/test/"},
            {
                "upstream": "SEC" if market == "US" else "Sina",
                "url": "https://data.sec.gov/test"
                if market == "US"
                else "https://quotes.sina.cn/test",
            },
        ],
    }


def test_automatic_can_value_without_claiming_human_review():
    for market, currency in [("US", "USD"), ("A", "CNY")]:
        spec = automatic(market)
        result = assess(10, currency, spec, "2026-10-30")
        assert result["status"] == "有安全边际"
        assert result["verification_mode"] == "automatic" and "未经人工" in result["review_note"]
        assert "reviewed" not in spec
        assert result["values"]["bear"] < result["values"]["base"] < result["values"]["bull"]
        assert result["rate_sensitivity"]["0.02"] < result["rate_sensitivity"]["0"]
        assert result["required_margin"] == 0.3


@pytest.mark.parametrize(
    "change",
    [
        "conflict",
        "negative",
        "debt",
        "future",
        "stale",
        "same_source",
        "currency",
        "infinite",
        "missing",
        "gap",
    ],
)
def test_automatic_bad_inputs_never_become_buy(change):
    spec = automatic()
    currency = "USD"
    if change == "conflict":
        spec["secondary"][0]["cfo"] *= 2
    elif change == "negative":
        for rows in [spec["primary"], spec["secondary"]]:
            rows[0]["cfo"] = -100
    elif change == "debt":
        for rows in [spec["primary"], spec["secondary"]]:
            rows[0]["debt"] = 3000
    elif change == "future":
        spec["checked_at"] = "2026-10-31"
    elif change == "stale":
        spec["checked_at"] = "2026-10-01"
    elif change == "same_source":
        spec["sources"][1]["upstream"] = "Yahoo Finance"
    elif change == "currency":
        currency = "CNY"
    elif change == "infinite":
        spec["primary"][0]["cfo"] = float("inf")
    elif change == "missing":
        del spec["primary"][0]["shares"]
    elif change == "gap":
        for rows in [spec["primary"], spec["secondary"]]:
            rows[-1]["date"] = "2022-12-31"
    assert assess(1, currency, spec, "2026-10-30")["status"] == "待核验"


def test_no_excess_cash_bonus_and_sbc_is_a_cost():
    spec = automatic()
    a = automatic_assessment(10, "USD", spec, "2026-10-30")
    for rows in [spec["primary"], spec["secondary"]]:
        for row in rows:
            row["cash"] *= 10
    b = automatic_assessment(10, "USD", spec, "2026-10-30")
    assert a["values"] == b["values"]
    for rows in [spec["primary"], spec["secondary"]]:
        for row in rows:
            row["sbc"] = 300
    assert (
        automatic_assessment(10, "USD", spec, "2026-10-30")["values"]["base"] < a["values"]["base"]
    )


def test_sec_rejects_quarter_ytd_future_filings_and_wrong_units():
    facts = {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {
                "USD": [
                    {
                        "val": 900,
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "filed": "2026-02-01",
                        "form": "10-K",
                    },
                    {
                        "val": 999,
                        "start": "2025-10-01",
                        "end": "2025-12-31",
                        "filed": "2026-02-01",
                        "form": "10-K",
                    },
                    {
                        "val": 1200,
                        "start": "2025-01-01",
                        "end": "2025-12-31",
                        "filed": "2026-11-01",
                        "form": "10-K/A",
                    },
                ]
            }
        }
    }
    assert (
        sec_fact(
            facts, ["NetCashProvidedByUsedInOperatingActivities"], "2025-12-31", "2026-10-30", True
        )
        == 900
    )
    with pytest.raises(FinancialError):
        sec_fact(facts, ["Absent"], "2025-12-31", "2026-10-30", True)


def test_refresh_failure_not_zero_or_reviewed_and_cache_expires(tmp_path, monkeypatch):
    fetch = Mock(side_effect=RuntimeError("https://SECRET"))
    monkeypatch.setattr("sequoia_x.reporting.fundamentals.yahoo_statements", fetch)
    f = Fundamentals(tmp_path / "fundamentals.json")
    first = f.refresh("US", ["MSFT"], "2026-10-30")["US:MSFT"]
    assert "SECRET" not in str(first) and "blocked_reason" in first and "primary" not in first
    f.refresh("US", ["MSFT"], "2026-10-30")
    assert fetch.call_count == 1
    f.refresh("US", ["MSFT"], "2026-11-02")
    assert fetch.call_count == 2
    financial = f.refresh("US", ["JPM"], "2026-11-02")["US:JPM"]
    assert "专用模型" in financial["blocked_reason"]


def test_sec_fiscal_mapping_and_funded_debt_are_unambiguous():
    from sequoia_x.reporting.fundamentals import sec_statements
    end='2025-12-28';facts={}
    def fact(tag,val,annual=False,unit='USD'):
        row={'end':end,'filed':'2026-02-01','form':'10-K','val':val}
        if annual:row['start']='2024-12-29'
        facts[tag]={'units':{unit:[row]}}
    for tag,val in [('NetCashProvidedByUsedInOperatingActivities',1300),('PaymentsToAcquireProductiveAssets',200),('NetIncomeLoss',1100),('ProfitLoss',1200),('Revenues',5000),('ShareBasedCompensation',100)]:fact(tag,val,True)
    for tag,val in [('DebtCurrent',20),('LongTermDebtNoncurrent',80),('CashCashEquivalentsAndShortTermInvestments',2000),('OperatingLeaseLiability',30),('FinanceLeaseLiability',40)]:fact(tag,val)
    fact('CommonStockSharesOutstanding',100,unit='shares')
    get=Mock(return_value=Mock(ok=True,json=lambda:{'facts':{'us-gaap':facts}}))
    rows,_=sec_statements('MSFT','2026-10-30',['2025-12-31'],get=get)
    assert rows[0]['reported_end']==end and rows[0]['date']=='2025-12-31'
    assert rows[0]['debt']==100 and rows[0]['lease_buffer']==70
    assert rows[0]['capex']==200
    facts['CashCashEquivalentsAndShortTermInvestments']['units']['USD']=[]
    with pytest.raises(FinancialError,match='CashAndCashEquivalents'):
        sec_statements('MSFT','2026-10-30',['2025-12-31'],get=get)


def test_lease_buffer_uses_larger_source_and_blocks_leveraged_equity():
    spec=automatic()
    spec['primary'][0]['lease_buffer']=1
    spec['secondary'][0]['lease_buffer']=2000
    assert assess(1,'USD',spec,'2026-10-30')['status']=='待核验'


def test_yahoo_canonical_debt_not_vendor_total_debt(monkeypatch):
    from sequoia_x.reporting.fundamentals import yahoo_statements
    import yfinance as yf
    dates=pd.to_datetime(['2025-12-31','2024-12-31','2023-12-31'])
    def df(values):return pd.DataFrame({d:values for d in dates})
    t=Mock()
    t.cash_flow=df({'Operating Cash Flow':1300,'Capital Expenditure':-200,'Stock Based Compensation':100})
    t.income_stmt=df({'Net Income Including Noncontrolling Interests':1200,'Net Income Common Stockholders':1100,'Total Revenue':5000})
    t.balance_sheet=df({'Ordinary Shares Number':100,'Cash Cash Equivalents And Short Term Investments':2000,'Cash And Cash Equivalents':1000,'Total Debt':999,'Current Debt':20,'Long Term Debt':80,'Capital Lease Obligations':30})
    t.fast_info={'currency':'USD'}
    t.history.return_value=pd.DataFrame({'Stock Splits':[0]},index=pd.to_datetime(['2026-10-30']))
    monkeypatch.setattr(yf,'Ticker',lambda *a:t)
    rows,_=yahoo_statements('MSFT','US','2026-10-30')
    assert rows[0]['debt']==100 and rows[0]['cash']==2000 and rows[0]['lease_buffer']==30
