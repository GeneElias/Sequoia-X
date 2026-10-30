from unittest.mock import Mock

import pandas as pd
import pytest

from sequoia_x.reporting.market import History, MarketData, MarketDataError


def test_tencent_keeps_raw_prices_and_reported_turnover(monkeypatch):
    raw = [
        ["2026-10-29", "99", "100", "102", "98", "1000"],
        ["2026-10-30", "100", "102", "103", "99", "2000"],
    ]
    adjusted = [
        ["2026-10-29", "49.5", "50", "51", "49", "1000"],
        ["2026-10-30", "50", "51", "51.5", "49.5", "2000"],
    ]
    quote = ["0"] * 40
    quote[3] = "102"
    quote[6] = "2000"
    quote[30] = "20261030160000"
    quote[37] = "2020"
    get = Mock(
        side_effect=[
            Mock(json=lambda: {"data": {"sz300750": {"qfqday": adjusted}}}),
            Mock(json=lambda: {"data": {"sz300750": {"day": raw, "qt": {"sz300750": quote}}}}),
        ]
    )
    monkeypatch.setattr("requests.get", get)
    h = MarketData._tencent("300750", "A", "2026-10-30")
    assert h.frame.loc["2026-10-30", "close"] == 51
    assert h.frame.loc["2026-10-30", "raw_close"] == 102
    assert h.frame.loc["2026-10-30", "volume"] == 200000
    assert h.frame.loc["2026-10-30", "amount"] == 20200000
    assert h.frame.loc["2026-10-29", "amount"] != h.frame.loc["2026-10-29", "amount"]
    assert all(call.kwargs["timeout"] == 12 for call in get.call_args_list)


def test_tencent_unknown_adjustment_not_silently_promoted(monkeypatch):
    get = Mock(
        return_value=Mock(
            json=lambda: {"data": {"sz300750": {"day": [["2026-10-30", 1, 1, 1, 1, 1]]}}}
        )
    )
    monkeypatch.setattr("requests.get", get)
    with pytest.raises(ValueError, match="显式复权"):
        MarketData._tencent("300750", "A", "2026-10-30")


def test_failed_adapters_expose_safe_validation_reason_not_vendor_secret(tmp_path, monkeypatch):
    provider = MarketData(tmp_path)
    primary = Mock(side_effect=RuntimeError("https://SECRET"))
    primary.__name__ = "_yahoo"
    secondary = Mock(side_effect=ValueError("无效低价"))
    secondary.__name__ = "_sina"
    monkeypatch.setattr(provider, "_yahoo", primary)
    monkeypatch.setattr(provider, "_sina", secondary)
    with pytest.raises(MarketDataError) as exc:
        provider.history("MSFT", "US", "2026-10-30")
    assert "SECRET" not in str(exc.value) and "无效低价" in str(exc.value)


def test_a_raw_quotes_can_use_independent_event_coverage(tmp_path, monkeypatch):
    provider = MarketData(tmp_path)
    frame = pd.DataFrame(
        {
            "raw_close": [100.0, 102.0],
            "raw_open": [99.0, 100.0],
            "raw_high": [101.0, 103.0],
            "raw_low": [98.0, 99.0],
        },
        index=["2026-10-29", "2026-10-30"],
    )
    h = History("300750", "A", frame, "Tencent", "Tencent", "consistent", "CNY")
    ev = History(
        "300750",
        "A",
        pd.DataFrame(
            {"raw_close": [100.0, 102.0], "stock_splits": [0.0, 0.0]},
            index=pd.to_datetime(frame.index),
        ),
        "yfinance",
        "Yahoo Finance",
        "consistent",
        "CNY",
    )
    monkeypatch.setattr("sequoia_x.reporting.market.validate_window", lambda *a: None)
    monkeypatch.setattr(provider, "history", lambda *a: h)
    monkeypatch.setattr(provider, "_yahoo", lambda *a: ev)
    assert provider.trading_history("300750", "A", "2026-10-30").frame.stock_splits.tolist() == [
        0,
        0,
    ]
    ev.frame["raw_close"] = [100.0, 120.0]
    with pytest.raises(ValueError, match="差异超过1%"):
        provider.trading_history("300750", "A", "2026-10-30")


def test_extra_raw_history_does_not_require_unavailable_old_event_rows(tmp_path, monkeypatch):
    import exchange_calendars as xc

    index = (
        xc.get_calendar("XSHG", start="2026-01-01", end="2026-10-30")
        .sessions[-140:]
        .strftime("%Y-%m-%d")
    )
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10000.0,
            "raw_open": 100.0,
            "raw_high": 101.0,
            "raw_low": 99.0,
            "raw_close": 100.0,
        },
        index=index,
    )
    h = History(
        "300750",
        "A",
        pd.concat([frame.iloc[:1].set_axis(["2024-01-02"]), frame]),
        "Tencent",
        "Tencent",
        "consistent",
        "CNY",
    )
    events = frame[["raw_close"]].copy()
    events["stock_splits"] = 0.0
    events.iloc[0, 0] = 50.0  # Old Yahoo Close can be split-adjusted; today's must agree.
    ev = History("300750", "A", events, "yfinance", "Yahoo Finance", "consistent", "CNY")
    provider = MarketData(tmp_path)
    monkeypatch.setattr(provider, "history", lambda *a: h)
    monkeypatch.setattr(provider, "_yahoo", lambda *a: ev)
    result = provider.trading_history("300750", "A", "2026-10-30")
    assert len(result.frame) == 140 and result.frame.index[-1] == "2026-10-30"
