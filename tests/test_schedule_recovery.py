from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest

from sequoia_x.reporting.delivery import DeliveryError, send_cards, with_dashboard
from sequoia_x.reporting.github_runner import execute
from sequoia_x.reporting.paper import PaperBook
from sequoia_x.reporting.scheduling import all_sent, due_tasks, task_info

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-only-not-a-real-key"
DASH = "https://geneelias.github.io/Sequoia-X/"
CARD = {
    "msg_type": "interactive",
    "card": {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "test"}}]},
}


def test_deadlines_holidays_and_overdue_recovery():
    # 07:00 Shanghai precedes the new US report deadline, but A is overdue.
    now = datetime(2026, 10, 29, 23, tzinfo=timezone.utc)
    assert "daily_US" not in due_tasks(now) and "daily_A" in due_tasks(now)
    assert "daily_US" in due_tasks(datetime(2026, 10, 30, 0, tzinfo=timezone.utc))
    identity, deadline, day = task_info("daily_A", now)
    assert identity == "daily_A:2026-10-29" and deadline.hour == 17
    assert task_info("weekly", datetime(2026, 10, 31, 0, tzinfo=timezone.utc))[2] == "2026-10-23"
    assert task_info("weekly", datetime(2026, 10, 31, 2, tzinfo=timezone.utc))[2] == "2026-10-30"


def test_completion_receipts_accept_legacy_button_without_new_message(tmp_path):
    decorated = with_dashboard([CARD], DASH)
    decorated[0]["card"]["elements"][-1]["actions"][0]["text"]["content"] = "打开看板（公开演示）"
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"code": 0}))
    send_cards(decorated, URL, tmp_path / "_state/delivery.sqlite", post=post)
    assert all_sent(tmp_path, [CARD], URL, DASH)
    assert not all_sent(tmp_path, [{**CARD, "extra": "changed"}], URL, DASH)


def test_completed_job_is_not_regenerated_or_resent(tmp_path, monkeypatch):
    now = datetime(2026, 10, 30, 3, tzinfo=timezone.utc)
    state = Mock(root=tmp_path)
    directory = tmp_path / "paper/daily/2026-10-29-US"
    directory.mkdir(parents=True)
    (directory / "cards.json").write_text(json.dumps([CARD]))
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"code": 0}))
    send_cards([CARD], URL, tmp_path / "_state/delivery.sqlite", post=post)
    monkeypatch.setenv("FEISHU_REPORT_WEBHOOK_URL", URL)
    monkeypatch.delenv("FEISHU_DASHBOARD_URL", raising=False)
    sim = Mock(side_effect=AssertionError("must not generate"))
    monkeypatch.setattr("sequoia_x.reporting.github_runner.Simulation", sim)
    assert execute(state, "daily_US", now)["state"] == "already_delivered"
    assert execute(state, "daily_US", now)["state"] == "already_completed"
    assert post.call_count == 1


def test_unknown_card_resumes_same_hash_and_is_not_regenerated(tmp_path, monkeypatch):
    now = datetime(2026, 10, 30, 3, tzinfo=timezone.utc)
    state = Mock(root=tmp_path)
    directory = tmp_path / "paper/daily/2026-10-29-US"
    directory.mkdir(parents=True)
    artifact = directory / "cards.json"
    artifact.write_text(json.dumps([CARD]))
    monkeypatch.setenv("FEISHU_REPORT_WEBHOOK_URL", URL)
    monkeypatch.delenv("FEISHU_DASHBOARD_URL", raising=False)
    with pytest.raises(DeliveryError):
        send_cards(
            [CARD], URL, tmp_path / "_state/delivery.sqlite", post=Mock(side_effect=TimeoutError)
        )
    sim = Mock()
    sim.deliver.side_effect = DeliveryError("unknown")
    monkeypatch.setattr("sequoia_x.reporting.github_runner.Simulation", Mock(return_value=sim))
    before = artifact.read_bytes()
    with pytest.raises(DeliveryError):
        execute(state, "daily_US", now)
    sim.daily.assert_not_called()
    assert artifact.read_bytes() == before


def test_delayed_signal_after_execution_open_is_not_hindsight_order(tmp_path, monkeypatch):
    from tests.test_paper import signal

    monkeypatch.setattr("sequoia_x.reporting.paper.latest_session", lambda *a: "2026-10-29")
    book = PaperBook(tmp_path / "paper.sqlite")
    report = signal(day="2026-10-29", market="A", symbol="600519")
    # The next session is underway but not closed: latest_session is still yesterday.
    assert book.register(report, now=datetime(2026, 10, 30, 2, tzinfo=timezone.utc)) == 0
    assert "未追溯" in report["markets"][0]["execution_blocked_reason"]
    with book.connect() as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 0


def test_daily_uses_automatic_material_to_register_real_paper_orders(tmp_path, monkeypatch):
    from copy import deepcopy
    from sequoia_x.reporting.simulation import Simulation
    from tests.test_reporting import history
    from tests.test_fundamentals import automatic

    now = datetime(2026, 10, 23, 22, tzinfo=timezone.utc)
    spec = automatic()
    spec["checked_at"] = "2026-10-23"
    spec["primary"][0]["capital_actions_through"] = "2026-10-23"
    monkeypatch.setitem(
        __import__("sequoia_x.reporting.report", fromlist=["DEFAULT_UNIVERSE"]).DEFAULT_UNIVERSE,
        "US",
        ["MSFT", "AAPL"],
    )

    def refresh(*a, **kw):
        return {f"US:{s}": deepcopy(spec) for s in ["MSFT", "AAPL"]}

    monkeypatch.setattr("sequoia_x.reporting.simulation.Fundamentals.refresh", refresh)
    sim = Simulation(tmp_path)

    def cheap_history(symbol, *a):
        h = history(symbol)
        h.frame[["open", "high", "low", "close", "raw_close"]] /= 10
        return h

    monkeypatch.setattr(sim.provider, "history", cheap_history)
    first = sim.daily("US", now=now)
    assert first["daily"][0]["new_orders"] == 2
    assert sim.daily("US", now=now)["daily"][0]["new_orders"] == 0
    with sim.book.connect() as c:
        assert c.execute("select count(*) from orders").fetchone()[0] == 2
        assert c.execute("select count(*) from fills").fetchone()[0] == 0
    report = json.loads((tmp_path / "paper/daily/2026-10-23-US/report.json").read_text())
    assert all(
        i["valuation"]["verification_mode"] == "automatic" for i in report["markets"][0]["items"]
    )


def test_financial_fetch_crossing_open_cannot_use_stale_start_time(tmp_path, monkeypatch):
    from sequoia_x.reporting.simulation import Simulation
    from tests.test_paper import signal

    clock = Mock()
    clock.now.side_effect = [
        datetime(2026, 10, 30, 13, 29, tzinfo=timezone.utc),
        datetime(2026, 10, 30, 13, 31, tzinfo=timezone.utc),
    ]
    monkeypatch.setattr("sequoia_x.reporting.simulation.datetime", clock)
    monkeypatch.setattr("sequoia_x.reporting.simulation.Fundamentals.refresh", lambda *a, **kw: {})
    monkeypatch.setattr(
        "sequoia_x.reporting.simulation.build_report", lambda *a, **kw: signal(day="2026-10-29")
    )
    monkeypatch.setattr(
        "sequoia_x.reporting.simulation.render_cards", lambda *a: [json.loads(json.dumps(CARD))]
    )
    monkeypatch.setattr("sequoia_x.reporting.simulation.render_markdown", lambda *a: "test")
    monkeypatch.setattr("sequoia_x.reporting.simulation.export_dashboard", lambda *a: None)
    sim = Simulation(tmp_path)
    result = sim.daily("US")
    assert result["daily"][0]["new_orders"] == 0
    report = json.loads((tmp_path / "paper/daily/2026-10-29-US/report.json").read_text())
    assert "晚于" in report["markets"][0]["execution_blocked_reason"]
