"""Data quality is independent of HTTP success; no real network or messages."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from sequoia_x.reporting.delivery import DeliveryError, send_cards
from sequoia_x.reporting.github_runner import execute
from sequoia_x.reporting.github_state import StateError
from sequoia_x.reporting.market import MarketDataError, normalize
from tests.test_reporting import history
from tests.test_schedule_recovery import CARD, URL

DAY = "2026-10-30"
IDENTITY = "daily_US:" + DAY
NOW = datetime(2026, 10, 31, 1, 30, tzinfo=timezone.utc)


def setup(tmp_path, monkeypatch, outcomes, old_bad=False):
    monkeypatch.setenv("FEISHU_REPORT_WEBHOOK_URL", URL)
    monkeypatch.delenv("FEISHU_DASHBOARD_URL", raising=False)
    folder = tmp_path / "paper/daily" / f"{DAY}-US"
    folder.mkdir(parents=True)
    (tmp_path / "_state").mkdir()
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"code": 0}))
    if old_bad:
        (folder / "report.json").write_text(json.dumps({"markets": [{"complete": False}]}))
        (folder / "cards.json").write_text(json.dumps([CARD]))
        send_cards([CARD], URL, tmp_path / "_state/delivery.sqlite", post=post)
        (tmp_path / "_state/operations.json").write_text(json.dumps({IDENTITY: {"phase": "done"}}))
    sim = Mock()

    def generate(*args, **kwargs):
        outcome = outcomes.pop(0)
        (folder / "report.json").write_text(
            json.dumps({"markets": [{"complete": outcome is True}]})
        )
        if outcome == "crash":
            raise RuntimeError("generation interrupted after report.json")
        card = deepcopy(CARD)
        card["card"]["elements"][0]["text"]["content"] = (
            f"report generation {sim.daily.call_count}: {outcome}"
        )
        (folder / "cards.json").write_text(json.dumps([card]))

    sim.daily.side_effect = generate
    sim.deliver.side_effect = lambda cards: {
        "status": "attempted",
        **send_cards(cards, URL, tmp_path / "_state/delivery.sqlite", post=post),
    }
    monkeypatch.setattr("sequoia_x.reporting.github_runner.Simulation", Mock(return_value=sim))
    return Mock(root=tmp_path), sim, post, folder


def test_old_done_but_bad_report_is_rechecked_then_corrected_once(tmp_path, monkeypatch):
    state, sim, post, folder = setup(tmp_path, monkeypatch, [False, True], old_bad=True)
    assert execute(state, "daily_US", NOW)["state"] == "awaiting_data"
    assert post.call_count == 1  # Old anomaly was already sent; no second anomaly.
    assert execute(state, "daily_US", NOW + timedelta(minutes=10))["state"] == "retry_scheduled"
    assert sim.daily.call_count == 1
    assert execute(state, "daily_US", NOW + timedelta(minutes=31))["state"] == "persisted_encrypted"
    assert post.call_count == 2
    assert "数据恢复更新" in json.dumps(post.call_args.kwargs["json"], ensure_ascii=False)
    assert execute(state, "daily_US", NOW + timedelta(minutes=32))["state"] == "already_completed"
    assert post.call_count == 2 and sim.daily.call_count == 2


def test_first_failure_alert_once_then_exponential_backoff(tmp_path, monkeypatch):
    state, sim, post, _ = setup(tmp_path, monkeypatch, [False, False, True])
    first = execute(state, "daily_US", NOW)
    assert first["state"] == "awaiting_data" and post.call_count == 1
    assert "不能解释为没有机会或零收益" in json.dumps(
        post.call_args.kwargs["json"], ensure_ascii=False
    )
    second = execute(state, "daily_US", NOW + timedelta(minutes=31))
    assert second["state"] == "awaiting_data" and post.call_count == 1
    assert datetime.fromisoformat(second["next_retry_at"]) == NOW + timedelta(minutes=91)
    assert execute(state, "daily_US", NOW + timedelta(minutes=61))["state"] == "retry_scheduled"
    assert execute(state, "daily_US", NOW + timedelta(minutes=92))["state"] == "persisted_encrypted"
    assert post.call_count == 2


def test_interrupted_generation_cannot_adopt_stale_sent_cards(tmp_path, monkeypatch):
    state, sim, post, folder = setup(tmp_path, monkeypatch, ["crash", True], old_bad=True)
    with pytest.raises(RuntimeError):
        execute(state, "daily_US", NOW)
    op = json.loads((tmp_path / "_state/operations.json").read_text())[IDENTITY]
    assert op["phase"] == "generating"
    assert json.loads((folder / "report.json").read_text())["markets"][0]["complete"] is False
    # Emulate new valid report.json reaching disk before the previous crash.
    (folder / "report.json").write_text(json.dumps({"markets": [{"complete": True}]}))
    assert execute(state, "daily_US", NOW + timedelta(minutes=1))["state"] == "persisted_encrypted"
    assert sim.daily.call_count == 2 and post.call_count == 2


def test_missing_prepared_cards_fail_closed(tmp_path, monkeypatch):
    state, sim, post, _ = setup(tmp_path, monkeypatch, [])
    (tmp_path / "_state/operations.json").write_text(json.dumps({IDENTITY: {"phase": "prepared"}}))
    with pytest.raises(StateError, match="卡片缺失"):
        execute(state, "daily_US", NOW)
    sim.daily.assert_not_called()
    post.assert_not_called()


def test_unknown_anomaly_is_not_regenerated_to_bypass_receipt(tmp_path, monkeypatch):
    state, sim, post, folder = setup(tmp_path, monkeypatch, [])
    (folder / "report.json").write_text(json.dumps({"markets": [{"complete": False}]}))
    (folder / "cards.json").write_text(json.dumps([CARD]))
    (tmp_path / "_state/operations.json").write_text(json.dumps({IDENTITY: {"phase": "prepared"}}))
    with pytest.raises(DeliveryError):
        send_cards(
            [CARD], URL, tmp_path / "_state/delivery.sqlite", post=Mock(side_effect=TimeoutError)
        )
    before = (folder / "cards.json").read_bytes()
    with pytest.raises(DeliveryError):
        execute(state, "daily_US", NOW)
    assert (folder / "cards.json").read_bytes() == before
    sim.daily.assert_not_called()
    post.assert_not_called()


def test_nonfinite_error_records_safe_field_and_date_without_filling():
    frame = history().frame
    frame.loc["2026-10-23", "close"] = float("nan")
    frame["vendor_detail"] = "https://SECRET"
    with pytest.raises(MarketDataError) as exc:
        normalize(frame, "2026-10-23")
    assert "close" in str(exc.value) and "2026-10-23" in str(exc.value)
    assert "SECRET" not in str(exc.value)
    assert frame.loc["2026-10-23", "close"] != frame.loc["2026-10-23", "close"]


def test_valid_pool_does_not_hide_unknown_portfolio_mark(tmp_path):
    from sequoia_x.reporting.github_runner import data_complete

    folder = tmp_path / "paper/daily" / f"{DAY}-US"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(json.dumps({"markets": [{"complete": True}]}))
    (folder / "paper.json").write_text(json.dumps({"complete": False, "equity": None}))
    assert data_complete(tmp_path, "daily_US", DAY) is False
