"""Zero candidates must not be presented as proof that no stock is worth buying."""

from copy import deepcopy

from sequoia_x.reporting.fundamentals import owner_cash, number
from sequoia_x.reporting.report import build_report, render_cards, render_markdown
from tests.test_reporting import history, valuation


def report(monkeypatch, values):
    monkeypatch.setattr("sequoia_x.reporting.report.latest_session", lambda *a: "2026-10-23")

    class Provider:
        def history(self, symbol, *args):
            return history(symbol)

    return build_report({"US": ["MSFT", "AAPL"]}, Provider(), values)


def test_technical_hits_with_missing_valuations_are_coverage_gap(monkeypatch):
    r = report(monkeypatch, {})
    group = r["markets"][0]
    h = group["research_health"]
    assert h["technical_hits"] == 2 and h["technical_valued"] == 0 and h["technical_unvalued"] == 2
    assert not h["coverage_complete"] and group["candidate_count"] == 0
    text = render_markdown(r)
    assert "研究覆盖不足" in text and "不能据此判断" in text
    assert any(
        "研究覆盖不足" in e["text"]["content"]
        for c in render_cards(r)
        for e in c["card"]["elements"]
    )


def test_full_review_is_still_limited_to_observation_pool(monkeypatch):
    r = report(monkeypatch, {"US:MSFT": valuation(), "US:AAPL": valuation()})
    h = r["markets"][0]["research_health"]
    assert h["coverage_complete"] and h["technical_valued"] == 2
    assert "全市场结论" in h["conclusion"]


def test_unknown_valuation_verdict_never_becomes_recommendation(monkeypatch):
    monkeypatch.setattr(
        "sequoia_x.reporting.report.assess",
        lambda *a: {"status": "new_unknown_status", "reason": "not verified"},
    )
    r = report(monkeypatch, {})
    assert r["markets"][0]["candidate_count"] == 0
    assert all("待核验" in i["status"] for i in r["markets"][0]["items"])


def test_noncontrolling_losses_do_not_make_positive_parent_profit_invalid():
    row = {"cfo": 1000, "capex": 100, "sbc": 0, "net_income": 800, "common_income": 850}
    assert owner_cash(row) == number(850)
    row["cfo"] = 800
    assert owner_cash(row) == number(700)  # No allocation above 100%.
