"""Offline tests of data integrity, valuation gates, card delivery and CLI."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from sequoia_x.reporting.__main__ import main
from sequoia_x.reporting.delivery import DeliveryError, send_cards
from sequoia_x.reporting.market import History, MarketData, latest_session, normalize, validate_window
from sequoia_x.reporting.report import build_report, indicators, render_cards, render_markdown
from sequoia_x.reporting.valuation import assess, dcf, number


def history(symbol="MSFT", stale=False):
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS", start="2026-01-01", end="2026-10-23")
    index = cal.sessions[cal.sessions <= pd.Timestamp("2026-10-22" if stale else "2026-10-23")][-140:]
    frame = pd.DataFrame({"open": 99., "high": 101., "low": 98., "close": 100.,
                          "volume": 1000., "raw_close": 100.}, index=index)
    frame.iloc[-1] = [101., 112., 100., 110., 2000., 110.]
    frame = normalize(frame, "2026-10-23")
    return History(symbol, "US", frame, "fixture", "fixture", "consistent", "USD")


def valuation():
    return {"method": "fcff_dcf", "currency": "USD", "reviewed": True,
            "reviewed_at": "2026-10-23", "financial_as_of": "2026-06-30",
            "fcff": "1000000", "shares": "100000", "net_debt": "0",
            "discount_rate": "0.10", "terminal_growth": "0.02", "years": 5,
            "growth": {"bear": "0", "base": "0.08", "bull": "0.15"},
            "required_margin": "0.2", "quality_status": "pass",
            "quality_reason": "Test fixture, not a real company assessment",
            "assumptions_reason": "Test assumptions, not market data",
            "sources": [{"upstream": "SEC", "url": "https://www.sec.gov/example"},
                        {"upstream": "Other", "url": "https://example.com/financials"}],
            "source_checks": [{"field": "fcff", "primary": "1000000", "secondary": "1000000"},
                              {"field": "shares", "primary": "100000", "secondary": "100000"},
                              {"field": "net_debt", "primary": "0", "secondary": "0"}]}


def test_closed_session_handles_half_day_holiday_and_weekend():
    # Thanksgiving Thursday closed; Friday early close at 18:00 UTC, then 30m buffer.
    before = datetime(2025, 11, 28, 18, 15, tzinfo=timezone.utc)
    after = datetime(2025, 11, 28, 18, 45, tzinfo=timezone.utc)
    assert latest_session("US", before) == "2025-11-26"
    assert latest_session("US", after) == "2025-11-28"
    assert latest_session("A", datetime(2026, 10, 24, 8, tzinfo=timezone.utc)) == "2026-10-23"


def test_filters_intraday_bars_and_rejects_bad_ohlc():
    frame = history().frame
    assert normalize(frame, "2026-10-22").index[-1] == "2026-10-22"
    bad = frame.copy()
    bad.loc[bad.index[-1], "high"] = 1
    with pytest.raises(ValueError):
        normalize(bad, "2026-10-23")
    with pytest.raises(ValueError):
        normalize(pd.concat([frame, frame.tail(1)]), "2026-10-23")


def test_missing_session_is_not_silently_used_for_120_day_return():
    frame = history().frame.drop(history().frame.index[-40])
    with pytest.raises(ValueError, match="不连续"):
        validate_window(frame, "US", "2026-10-23")


def test_primary_failure_routes_to_backup_without_faking_adjustment(tmp_path, monkeypatch):
    provider = MarketData(tmp_path)
    primary, backup = Mock(side_effect=requests.Timeout), Mock(return_value=history())
    primary.__name__, backup.__name__ = "_yahoo", "_sina"
    backup.return_value.adjustment = "unknown"
    monkeypatch.setattr(provider, "_yahoo", primary)
    monkeypatch.setattr(provider, "_sina", backup)
    result = provider.history("MSFT", "US", "2026-10-23")
    assert result.adjustment == "unknown" and result.warnings
    assert not list(tmp_path.glob("*.json"))


def test_stale_primary_not_accepted_as_ready(tmp_path, monkeypatch):
    provider = MarketData(tmp_path)
    primary = Mock(return_value=history(stale=True)); primary.__name__ = "_yahoo"
    backup = Mock(side_effect=ValueError); backup.__name__ = "_sina"
    monkeypatch.setattr(provider, "_yahoo", primary)
    monkeypatch.setattr(provider, "_sina", backup)
    with pytest.raises(ValueError):
        provider.history("MSFT", "US", "2026-10-23")


def test_valuation_requires_review_sources_and_fresh_inputs():
    assert assess(100, "USD", None, "2026-10-23")["status"] == "待核验"
    spec = valuation()
    result = assess(100, "USD", spec, "2026-10-23")
    assert result["values"]["bear"] < result["values"]["base"] < result["values"]["bull"]
    assert result["values"]["base"] == pytest.approx(float(dcf(spec, number(".08"))))
    for key, value in [("reviewed", False), ("currency", "CNY"), ("reviewed_at", "2020-01-01"),
                       ("discount_rate", ".03")]:
        broken = {**spec, key: value}
        assert assess(100, "USD", broken, "2026-10-23")["status"] == "待核验"
    spec["sources"][1]["url"] = "https://data.sec.gov/example"
    assert assess(100, "USD", spec, "2026-10-23")["status"] == "待核验"


def test_quality_veto_and_reverse_dcf():
    spec = valuation()
    spec["quality_status"] = "fail"
    assert assess(100, "USD", spec, "2026-10-23")["status"] == "否决"
    spec["quality_status"] = "pass"
    price = float(dcf(spec, number(".08")))
    result = assess(price, "USD", spec, "2026-10-23")
    assert result["reverse_growth"] == pytest.approx(.08)
    assert result["margin"] == pytest.approx(0, abs=1e-10)
    assert result["rate_sensitivity"]["0.02"] < result["rate_sensitivity"]["0"]


def test_source_conflict_blocks_valuation():
    spec = valuation()
    spec["source_checks"][0]["secondary"] = "2000000"
    assert assess(100, "USD", spec, "2026-10-23")["status"] == "待核验"


def test_model_inputs_must_match_reviewed_source_checks():
    spec = valuation()
    spec["fcff"] = "2000000"
    assert assess(100, "USD", spec, "2026-10-23")["status"] == "待核验"


def test_a_share_turnover_cannot_be_invented_from_adjusted_price():
    frame = history().frame
    assert indicators(frame, "US")["confirmed"]
    assert not indicators(frame, "A")["confirmed"]
    frame["amount"] = 200_000_000
    assert indicators(frame, "A")["confirmed"]


def make_report(monkeypatch, fail=False):
    monkeypatch.setattr("sequoia_x.reporting.report.latest_session", lambda *a: "2026-10-23")
    provider = Mock()
    provider.history.side_effect = lambda symbol, *args: history(symbol) if symbol != "FAIL" else (_ for _ in ()).throw(ValueError())
    pool = ["MSFT", "FAIL" if fail else "AAPL"]
    return build_report({"US": pool}, provider, {})


def test_missing_valuation_never_becomes_buy_candidate(monkeypatch):
    report = make_report(monkeypatch)
    assert report["markets"][0]["candidate_count"] == 0
    assert all(x["status"] == "待核验：估值未完成" for x in report["markets"][0]["items"])
    cards = render_cards(report)
    assert all(len(json.dumps(c, ensure_ascii=False).encode()) < 20000 for c in cards)
    assert "待核验" in render_markdown(report)
    assert "finance.yahoo.com/quote/MSFT/" in json.dumps(cards)


def test_partial_pool_disables_rps_and_reports_error(monkeypatch):
    report = make_report(monkeypatch, fail=True)
    group = report["markets"][0]
    assert not group["complete"] and group["candidate_count"] == 0
    assert group["items"][0]["rps"] is None
    assert "股票池不完整" in render_markdown(report)


def test_large_report_splits_cards_under_bot_limit(monkeypatch):
    report = make_report(monkeypatch)
    group = report["markets"][0]
    group["shown"] = [dict(group["shown"][0], symbol=f"SYM{i}", warnings=[]) for i in range(10)]
    for item in group["shown"]:
        item["valuation"] = assess(100, "USD", valuation(), "2026-10-23")
        item["valuation"]["reason"] = "逐项审阅。" * 50
    cards = render_cards(report)
    assert len(cards) > 1
    assert all(len(json.dumps(c, ensure_ascii=False).encode()) < 20000 for c in cards)


def test_complete_quality_and_valuation_can_produce_candidate(monkeypatch):
    monkeypatch.setattr("sequoia_x.reporting.report.latest_session", lambda *a: "2026-10-23")
    provider = Mock()
    provider.history.side_effect = lambda symbol, *args: history(symbol)
    report = build_report({"US": ["MSFT", "AAPL"]}, provider,
                          {"US:MSFT": valuation(), "US:AAPL": valuation()})
    assert report["markets"][0]["candidate_count"] == 2


def test_delivery_deduplicates_and_checks_business_response(tmp_path, monkeypatch):
    cards = render_cards(make_report(monkeypatch))
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"code": 0}))
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-not-a-real-key"
    ledger = tmp_path / "send.db"
    assert send_cards(cards, url, ledger, secret="test", post=post)["sent"] == len(cards)
    assert "sign" in post.call_args.kwargs["json"]
    assert send_cards(cards, url, ledger, post=post)["skipped"] == len(cards)
    assert post.call_count == len(cards)
    with pytest.raises(DeliveryError):
        send_cards(cards, url, ledger, force=True,
                   post=Mock(return_value=Mock(status_code=200, json=lambda: {"code": 19024})))


def test_timeout_is_unknown_and_does_not_auto_resend(tmp_path, monkeypatch):
    cards = render_cards(make_report(monkeypatch))
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-not-a-real-key"
    post = Mock(side_effect=requests.Timeout("SECRET_URL"))
    with pytest.raises(DeliveryError, match="结果不明") as exc:
        send_cards(cards, url, tmp_path / "send.db", post=post)
    assert "SECRET_URL" not in str(exc.value)
    with pytest.raises(DeliveryError, match="结果不明"):
        send_cards(cards, url, tmp_path / "send.db", post=post)
    assert post.call_count == 1


def test_cli_preview_and_missing_webhook(tmp_path, monkeypatch):
    report = make_report(monkeypatch)
    monkeypatch.setattr("sequoia_x.reporting.__main__.build_report", lambda *a, **kw: report)
    monkeypatch.setattr("sequoia_x.reporting.__main__.load_dotenv", lambda: None)
    monkeypatch.setattr("requests.post", Mock(side_effect=AssertionError("preview must not send")))
    assert main(["--market", "US", "--output", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("*/cards.json"))) == 1
    monkeypatch.delenv("FEISHU_REPORT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    assert main(["--market", "US", "--send", "--output", str(tmp_path)]) == 2
