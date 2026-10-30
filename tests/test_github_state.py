import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

Fernet = pytest.importorskip("cryptography.fernet").Fernet

from sequoia_x.reporting.delivery import DeliveryError, send_cards
from sequoia_x.reporting.github_state import GitHubState, StateError, encrypted_snapshot, restore_snapshot
from sequoia_x.reporting.paper import PaperBook


def state_root(path):
    PaperBook(path / "_state/paper.sqlite")
    report = path / "paper/daily/2026-10-23-US/report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"private_marker": "PRIVATE_PORTFOLIO_MARKER"}))
    (path / ".env").write_text("SECRET_MUST_NEVER_ARCHIVE")
    return path


def test_encrypted_roundtrip_excludes_env_and_key_files(tmp_path):
    key = Fernet.generate_key()
    root = state_root(tmp_path / "source")
    (root / "_state/github-state.key").write_bytes(key)
    for name in ('fundamentals.json','operations.json','diagnostics.json'):
        (root/'_state'/name).write_text('{"private":"PRIVATE_AUDIT_MARKER"}')
    encrypted = encrypted_snapshot(root, key)
    assert b"PRIVATE_PORTFOLIO_MARKER" not in encrypted and key not in encrypted
    restored = tmp_path / "restored"
    restore_snapshot(encrypted, restored, key)
    for name in ('fundamentals.json','operations.json','diagnostics.json'):
        assert json.loads((restored/'_state'/name).read_text())['private']=='PRIVATE_AUDIT_MARKER'
    assert b'PRIVATE_AUDIT_MARKER' not in encrypted
    assert not (restored / ".env").exists()
    assert not (restored / "_state/github-state.key").exists()
    assert "PRIVATE_PORTFOLIO_MARKER" in (restored / "paper/daily/2026-10-23-US/report.json").read_text()
    with sqlite3.connect(restored / "_state/paper.sqlite") as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_bad_key_fails_closed(tmp_path):
    root = state_root(tmp_path / "source")
    encrypted = encrypted_snapshot(root, Fernet.generate_key())
    target = tmp_path / "target"
    with pytest.raises(StateError, match="不能|不得"):
        restore_snapshot(encrypted, target, Fernet.generate_key())
    assert not target.exists()


def test_missing_book_cannot_be_saved_as_empty_state(tmp_path):
    with pytest.raises(StateError):
        encrypted_snapshot(tmp_path, Fernet.generate_key())


def local_repository(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    return repo


def test_state_branch_roundtrip_and_no_worktree_mutation(tmp_path):
    repo = local_repository(tmp_path)
    key = Fernet.generate_key()
    state = GitHubState(repo, state_root(tmp_path / "reports"), key)
    state.save(initialise=True)
    tree = state.git("ls-tree", "--name-only", state.tip).decode().splitlines()
    assert tree == ["README.md", "state.fernet"]
    assert list(repo.glob("*.sqlite")) == []
    restored = GitHubState(repo, tmp_path / "restore", key)
    restored.restore()
    restored.save()
    assert restored.tip != state.tip
    with pytest.raises(StateError):
        state.save()  # optimistic concurrency protects the newer remote tip
    with pytest.raises(StateError):
        state.save(initialise=True)


def test_missing_remote_never_auto_resets(tmp_path):
    repo = local_repository(tmp_path)
    state = GitHubState(repo, tmp_path / "root", Fernet.generate_key())
    with pytest.raises(StateError):
        state.restore()
    assert not (tmp_path / "root").exists()


def test_delivery_checkpoint_happens_before_http_and_after_success(tmp_path):
    path = tmp_path / "delivery.sqlite"
    states = []
    def checkpoint():
        with sqlite3.connect(path) as c:
            states.append(c.execute("SELECT state FROM deliveries").fetchone()[0])
    def post(*args, **kwargs):
        assert states == ["pending"]
        return Mock(status_code=200, json=lambda: {"code": 0})
    card = {"msg_type": "interactive", "card": {"elements": []}}
    result = send_cards([card], "https://open.feishu.cn/open-apis/bot/v2/hook/test-not-real", path,
                        checkpoint=checkpoint, post=post)
    assert result["sent"] == 1 and states == ["pending", "sent"]


def test_failed_pre_send_checkpoint_never_calls_remote(tmp_path):
    post = Mock()
    with pytest.raises(DeliveryError, match="未发送"):
        send_cards([{"msg_type": "interactive", "card": {"elements": []}}],
                   "https://open.feishu.cn/open-apis/bot/v2/hook/test-not-real", tmp_path / "send.sqlite",
                   post=post, checkpoint=Mock(side_effect=RuntimeError))
    post.assert_not_called()


def test_live_workflow_can_pause_is_serial_and_preserves_timezone():
    text = Path('.github/workflows/research-schedule.yml').read_text()
    assert "vars.SEQUOIA_ENABLED != 'false'" in text
    assert "cancel-in-progress: false" in text and "contents: write" in text
    assert all(cron in text for cron in ('0 9 * * 1-5','30 23 * * 1-5','0 1 * * 6','13,43 * * * *'))
    assert 'pull_request:' not in text and 'upload-artifact' not in text
