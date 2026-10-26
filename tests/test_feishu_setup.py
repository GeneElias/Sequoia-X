from unittest.mock import Mock

import pytest

from sequoia_x.reporting.delivery import DeliveryError, with_dashboard
from sequoia_x.reporting.report import content_id
from sequoia_x.reporting.simulation import Simulation


def card():
    return {"msg_type": "interactive", "card": {"elements": []}}


def test_optional_demo_button_is_hashed_without_mutating_original():
    original = [card()]
    result = with_dashboard(original, "https://geneelias.github.io/Sequoia-X/")
    assert original[0]["card"]["elements"] == []
    button = result[0]["card"]["elements"][0]["actions"][0]
    assert "公开演示" in button["text"]["content"]
    assert content_id(result[0]) != content_id(original[0])
    assert with_dashboard(original, None) is original


@pytest.mark.parametrize("url", ["http://localhost:8000", "https://example.com/", "https://user:secret@geneelias.github.io/", "https://geneelias.github.io/?token=secret"])
def test_demo_button_rejects_local_credential_or_unexpected_hosts(url):
    with pytest.raises(DeliveryError):
        with_dashboard([card()], url)


def test_running_daemon_reloads_local_config_instead_of_stale_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FEISHU_REPORT_WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/stale-test-value")
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_DASHBOARD_URL", raising=False)
    post = Mock(return_value={"sent": 1, "skipped": 0})
    monkeypatch.setattr("sequoia_x.reporting.simulation.send_cards", post)
    sim = Simulation(tmp_path / "reports", send=True)
    path = tmp_path / ".env"
    for suffix in ("first-test-value", "changed-test-value"):
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + suffix
        path.write_text("FEISHU_REPORT_WEBHOOK_URL=" + url)
        assert sim.deliver([card()])["sent"] == 1
        assert post.call_args.args[1] == url
    path.write_text("FEISHU_REPORT_WEBHOOK_URL=\n")
    assert sim.deliver([card()])["status"] == "blocked_missing_webhook"
    assert post.call_count == 2
