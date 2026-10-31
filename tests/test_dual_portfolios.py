"""Two independent books, no hindsight activation and safe encrypted persistence."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet

from sequoia_x.reporting.dashboard import dataset
from sequoia_x.reporting.github_runner import data_complete
from sequoia_x.reporting.github_state import encrypted_snapshot, restore_snapshot, StateError
from sequoia_x.reporting.paper import PaperBook, weekly_markdown
from sequoia_x.reporting.portfolios import ensure_portfolios
from tests.test_paper import quote, signal

NOW = datetime(2026, 11, 1, 0, tzinfo=timezone.utc)


def test_legacy_policy_adoption_preserves_positions_and_rejects_cross_portfolio(tmp_path):
    path = tmp_path / "paper.sqlite"
    book = PaperBook(path)
    with book.connect() as c:
        spec = json.loads(c.execute("SELECT spec FROM policy").fetchone()[0])
        del spec["portfolio"]
        c.execute("UPDATE policy SET spec=?", (json.dumps(spec, sort_keys=True),))
        c.execute("INSERT INTO positions VALUES ('US','MSFT','100','12345','2026-10-30')")
    with pytest.raises(ValueError):
        PaperBook(path, portfolio="technical")
    adopted = PaperBook(path)
    with adopted.connect() as c:
        assert c.execute("SELECT cost FROM positions").fetchone()[0] == "12345"
        assert (
            json.loads(c.execute("SELECT spec FROM policy").fetchone()[0])["portfolio"]
            == "strict_value"
        )


def test_activation_is_persistent_and_never_backfills(tmp_path):
    PaperBook(tmp_path / "_state/paper.sqlite")
    config = ensure_portfolios(tmp_path, NOW)
    assert config["technical_first_signal"] == {"A": "2026-11-02", "US": "2026-11-02"}
    assert ensure_portfolios(tmp_path, datetime(2026, 11, 5, tzinfo=timezone.utc)) == config
    technical = PaperBook(tmp_path / "_state/paper-technical.sqlite", portfolio="technical")
    with technical.connect() as c:
        assert c.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    technical.path.unlink()
    with pytest.raises(RuntimeError, match="拒绝"):
        ensure_portfolios(tmp_path, NOW)


def test_unverified_technical_signal_trades_only_in_technical_book(tmp_path):
    strict = PaperBook(tmp_path / "strict.sqlite")
    tech = PaperBook(tmp_path / "technical.sqlite", portfolio="technical")
    report = signal()
    report["markets"][0]["shown"][0].update(
        status="量价确认，估值待核验", valuation={"status": "待核验"}
    )
    now = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
    assert strict.register(report, now) == 0
    assert tech.register(report, now) == 1
    assert tech.register(report, now) == 0
    provider = Mock()
    provider.trading_history.return_value = quote()
    assert tech.settle("US", "2026-10-26", provider)["contributed"] == "10005.0000"
    assert strict.settle("US", "2026-10-26", provider)["contributed"] == "0"
    assert (
        tech.weekly("2026-10-30")["markets"][1]["complete"] is False
    )  # Missing Friday prices cannot become a gain.


def test_technical_filters_full_pool_and_limits_ten_without_valuation_bias(tmp_path):
    book = PaperBook(tmp_path / "technical.sqlite", portfolio="technical")
    report = signal()
    group = report["markets"][0]
    group["items"] = [
        {
            **deepcopy(group["shown"][0]),
            "symbol": f"T{i:02}",
            "score": i,
            "valuation": {"status": "待核验"},
        }
        for i in range(12)
    ]
    group["shown"] = []  # Picks are not restricted to the strict report's ten displayed rows.
    assert book.register(report, datetime(2026, 10, 23, 22, tzinfo=timezone.utc)) == 10
    with book.connect() as c:
        assert {r[0] for r in c.execute("SELECT symbol FROM orders")} == {
            f"T{i:02}" for i in range(2, 12)
        }
    group["complete"] = False
    assert book.register(report, datetime(2026, 10, 23, 22, tzinfo=timezone.utc)) == 0


def test_dual_ledger_encryption_roundtrip_and_missing_book_fail_closed(tmp_path):
    root = tmp_path / "source"
    PaperBook(root / "_state/paper.sqlite")
    ensure_portfolios(root, NOW)
    key = Fernet.generate_key()
    token = encrypted_snapshot(root, key)
    restored = tmp_path / "restored"
    restore_snapshot(token, restored, key)
    assert ensure_portfolios(restored, NOW) == ensure_portfolios(root, NOW)
    assert (restored / "_state/paper-technical.sqlite").exists()
    (root / "_state/paper-technical.sqlite").unlink()
    with pytest.raises(StateError):
        encrypted_snapshot(root, key)


def test_weekly_health_and_render_include_both_without_mixing_currency(tmp_path):
    directory = tmp_path / "paper/weekly/2026-11-06"
    directory.mkdir(parents=True)
    flat = {
        "week_start": "2026-11-02",
        "week_end": "2026-11-06",
        "basis": "price only",
        "markets": [
            {
                "market": "A",
                "currency": "CNY",
                "as_of": "2026-11-06",
                "complete": False,
                "reason": "missing",
            }
        ],
    }
    dual = {
        **flat,
        "portfolios": {"strict_value": flat, "technical": {"not_started": True, "markets": []}},
    }
    path = directory / "review.json"
    path.write_text(json.dumps(dual))
    assert not data_complete(tmp_path, "weekly", "2026-11-06")
    text = weekly_markdown(dual)
    assert "严格价值组合" in text and "量价实验组合" in text and "同币种" in text and "尚未" in text


def test_dashboard_separates_positions_and_keeps_old_alias(tmp_path):
    PaperBook(tmp_path / "_state/paper.sqlite")
    ensure_portfolios(tmp_path, NOW)
    technical = PaperBook(tmp_path / "_state/paper-technical.sqlite", portfolio="technical")
    with technical.connect() as c:
        c.execute("INSERT INTO snapshots VALUES ('US','2026-11-03','10500','10005','0','[]',1)")
    data = dataset(tmp_path)
    assert data["markets"]["US"]["history"] == []
    assert data["portfolios"]["strict_value"]["markets"]["US"]["history"] == []
    assert data["portfolios"]["technical"]["markets"]["US"]["history"][0]["pnl"] == 495


def test_daily_retry_and_weekly_review_keep_both_books_independent(tmp_path, monkeypatch):
    from sequoia_x.reporting.simulation import Simulation

    ensure_portfolios(tmp_path, datetime(2026, 10, 22, 22, tzinfo=timezone.utc))
    current = ["2026-10-23"]

    def build(*args, **kwargs):
        report = signal(day=current[0])
        group = report["markets"][0]
        group.update(pool_size=1, candidate_count=0)
        group["shown"][0].update(status="估值待核验", valuation={"status": "待核验"})
        group["items"] = group["shown"]
        return report

    monkeypatch.setattr("sequoia_x.reporting.simulation.Fundamentals.refresh", lambda *a: {})
    monkeypatch.setattr("sequoia_x.reporting.simulation.build_report", build)
    monkeypatch.setattr("sequoia_x.reporting.simulation.render_markdown", lambda *a: "test")
    monkeypatch.setattr(
        "sequoia_x.reporting.simulation.render_cards", lambda *a: [{"card": {"elements": []}}]
    )
    sim = Simulation(tmp_path)
    first = sim.daily("US", datetime(2026, 10, 23, 22, tzinfo=timezone.utc))
    assert first["daily"][0]["new_orders_by_portfolio"] == {"strict_value": 0, "technical": 1}
    again = sim.daily("US", datetime(2026, 10, 23, 22, tzinfo=timezone.utc))
    assert again["daily"][0]["new_orders_by_portfolio"] == {"strict_value": 0, "technical": 0}
    current[0] = "2026-10-26"
    monkeypatch.setattr(sim.provider, "trading_history", lambda *a: quote())
    sim.daily("US", datetime(2026, 10, 26, 22, tzinfo=timezone.utc))
    monkeypatch.setattr(sim.provider, "trading_history", lambda *a: quote("2026-10-30"))
    sim.weekly("2026-10-30", datetime(2026, 10, 31, 2, tzinfo=timezone.utc))
    review = json.loads((tmp_path / "paper/weekly/2026-10-30/review.json").read_text())
    strict = review["portfolios"]["strict_value"]["markets"][1]
    tech = review["portfolios"]["technical"]["markets"][1]
    assert strict["contributed"] == "0" and strict["weekly_return"] is None
    assert float(tech["weekly_pnl"]) == 495 and tech["fill_count"] == 1
    assert tech["weekly_return"] > 0
    technical_text = weekly_markdown(review["portfolios"]["technical"])
    assert "估值意见单独展示" in technical_text
    assert "不将观察项或估值待核验项" not in technical_text


def test_first_deployment_daily_does_not_register_old_technical_signal(tmp_path, monkeypatch):
    from sequoia_x.reporting.simulation import Simulation
    from tests.test_reporting import history

    monkeypatch.setattr("sequoia_x.reporting.simulation.Fundamentals.refresh", lambda *a: {})
    sim = Simulation(tmp_path)
    monkeypatch.setattr(sim.provider, "history", lambda symbol, *a: history(symbol))
    now = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
    result = sim.daily("US", now)
    assert result["daily"][0]["new_orders_by_portfolio"] == {"strict_value": 0, "technical": 0}
    technical = PaperBook(tmp_path / "_state/paper-technical.sqlite", portfolio="technical")
    with technical.connect() as c:
        assert c.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
