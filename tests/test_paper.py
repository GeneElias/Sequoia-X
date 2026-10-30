"""Forward-only paper trading and scheduled two-market weekly review."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd
import pytest

from sequoia_x.reporting.market import History
from sequoia_x.reporting.paper import PaperBook, next_session, weekly_cards, weekly_markdown
from sequoia_x.reporting.simulation import due_jobs, run_due


def signal(day="2026-10-23", market="US", symbol="MSFT", status="买入研究候选（非交易指令）"):
    return {"demo": False, "markets": [{"market": market, "as_of": day, "complete": True,
             "shown": [{"symbol": symbol, "date": day, "status": status, "confirmed": True,
                        "valuation": {"status": "有安全边际"}}]}]}


def quote(day="2026-10-26", price=105, split=0, market="US", locked=False):
    df = pd.DataFrame([
        {"raw_open": 99, "raw_high": 102, "raw_low": 98, "raw_close": 100,
         "volume": 1000, "stock_splits": 0},
        {"raw_open": price if locked else 100, "raw_high": price if locked else max(110,price),
         "raw_low": price if locked else 99, "raw_close": price, "volume": 1000, "stock_splits": split},
    ], index=["2026-10-23", day])
    return History("MSFT", market, df, "fixture", "fixture", "consistent", "USD" if market=="US" else "CNY")


def register(book, monkeypatch, day="2026-10-23", market="US", symbol="MSFT"):
    monkeypatch.setattr("sequoia_x.reporting.paper.latest_session", lambda *a: day)
    return book.register(signal(day, market, symbol), now=datetime.fromisoformat(day+"T22:00:00+00:00"))


def test_100_shares_next_open_and_idempotence(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    assert register(book, monkeypatch) == 1
    assert register(book, monkeypatch) == 0
    provider = Mock()
    provider.trading_history.return_value = quote()
    before = book.settle("US", "2026-10-23", provider)
    assert before["contributed"] == "0"
    provider.trading_history.assert_not_called()
    first = book.settle("US", "2026-10-26", provider)
    second = book.settle("US", "2026-10-26", provider)
    assert Decimal(first["contributed"]) == Decimal("10005")
    assert first["equity"] == second["equity"] == "10500"
    with book.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        assert c.execute("SELECT quantity FROM positions").fetchone()[0] == "100"


def test_repeated_recommendations_add_100_on_next_day(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    register(book, monkeypatch)
    provider = Mock()
    provider.trading_history.return_value = quote()
    book.settle("US", "2026-10-26", provider)
    register(book, monkeypatch, day="2026-10-26")
    provider.trading_history.return_value = quote("2026-10-27")
    book.settle("US", "2026-10-27", provider)
    with book.connect() as c:
        assert c.execute("SELECT quantity FROM positions").fetchone()[0] == "200"
        assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_late_job_does_not_hindsight_fill(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    register(book, monkeypatch)
    provider = Mock()
    provider.trading_history.return_value = quote("2026-10-27")
    result = book.settle("US", "2026-10-27", provider)
    assert result["orders"] == {"expired": 1}
    assert result["contributed"] == "0"


def test_observations_pending_values_and_demo_never_order(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    monkeypatch.setattr("sequoia_x.reporting.paper.latest_session", lambda *a: "2026-10-23")
    r = signal(status="待核验：估值未完成")
    assert book.register(r) == 0
    r["demo"] = True
    with pytest.raises(ValueError):
        book.register(r)
    r = signal(day="2026-10-22")
    with pytest.raises(ValueError, match="过期"):
        book.register(r)


def test_a_share_unfillable_limit_up_and_initial_star_lot(tmp_path, monkeypatch):
    for symbol, locked in [("600519", True), ("688001", False)]:
        book = PaperBook(tmp_path / f"{symbol}.db")
        register(book, monkeypatch, market="A", symbol=symbol)
        provider = Mock()
        provider.trading_history.return_value = quote(market="A", locked=locked)
        result = book.settle("A", "2026-10-26", provider)
        assert result["orders"] == {"unfilled": 1}
        assert result["contributed"] == "0"


def test_split_updates_position_once(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    register(book, monkeypatch)
    provider = Mock()
    provider.trading_history.return_value = quote()
    book.settle("US", "2026-10-26", provider)
    provider.trading_history.return_value = quote("2026-10-27", price=52.5, split=2)
    first = book.settle("US", "2026-10-27", provider)
    second = book.settle("US", "2026-10-27", provider)
    assert Decimal(first["equity"]) == Decimal(second["equity"]) == Decimal("10500")
    with book.connect() as c:
        assert c.execute("SELECT quantity FROM positions").fetchone()[0] == "200"


def test_weekly_cashflows_not_counted_as_profit(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    provider = Mock()
    for market in ("A", "US"):
        book.settle(market, "2026-10-23", provider)
    register(book, monkeypatch)
    provider.trading_history.return_value = quote()
    book.settle("US", "2026-10-26", provider)
    provider.trading_history.return_value = quote("2026-10-30")
    book.settle("US", "2026-10-30", provider)
    book.settle("A", "2026-10-30", provider)
    weekly = book.weekly("2026-10-30")
    a, us = weekly["markets"]
    assert a["currency"] == "CNY" and us["currency"] == "USD"
    assert a["weekly_return"] is None and a["weekly_pnl"] == "0"
    assert Decimal(us["weekly_pnl"]) == Decimal("495")
    assert us["weekly_return"] == pytest.approx(495 / 10005)
    assert len(weekly_cards(weekly)) >= 1
    assert "非时间加权" in weekly_markdown(weekly)


def test_missing_quote_is_not_zero_profit_or_zero_value(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    register(book, monkeypatch)
    provider = Mock()
    provider.trading_history.return_value = quote()
    book.settle("US", "2026-10-26", provider)
    provider.trading_history.side_effect = ValueError
    assert book.settle("US", "2026-10-30", provider)["equity"] is None
    assert not book.weekly("2026-10-30")["markets"][1]["complete"]


def test_weekly_requires_prior_week_baseline_for_existing_holdings(tmp_path, monkeypatch):
    book = PaperBook(tmp_path / "paper.db")
    register(book, monkeypatch)
    provider = Mock()
    provider.trading_history.return_value = quote()
    book.settle("US", "2026-10-26", provider)
    provider.trading_history.return_value = quote("2026-11-06")
    book.settle("US", "2026-11-06", provider)
    assert not book.weekly("2026-11-06")["markets"][1]["complete"]


def test_fee_policy_is_fixed_per_ledger(tmp_path):
    path = tmp_path / "paper.db"
    PaperBook(path)
    with pytest.raises(ValueError):
        PaperBook(path, slippage_bps="10")


def test_schedule_uses_shanghai_and_deduplicates_jobs(tmp_path):
    saturday = datetime(2026, 10, 24, 2, tzinfo=timezone.utc)
    assert [kind for _,kind in due_jobs(saturday)] == ["US", "weekly"]
    monday = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    assert [kind for _,kind in due_jobs(monday)] == ["A"]
    sim = Mock(root=tmp_path)
    (tmp_path / "_state").mkdir()
    sim.daily.return_value = {"ok": True}
    sim.weekly.return_value = {"ok": True}
    assert len(run_due(sim, saturday)) == 2
    assert run_due(sim, saturday) == []
    assert sim.daily.call_count == sim.weekly.call_count == 1


def test_next_session_skips_weekends_and_holidays():
    assert next_session("US", "2026-10-23") == "2026-10-26"
    assert next_session("US", "2025-11-26") == "2025-11-28"
    assert next_session("A", "2026-10-23") == "2026-10-26"


def test_cloud_copy_cannot_write_after_github_cutover(tmp_path, monkeypatch):
    import json
    from sequoia_x.reporting.simulation import Simulation
    marker = tmp_path / "_state/github-migration.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"phase": "github_active"}))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="GitHub接管"):
        Simulation(tmp_path)
