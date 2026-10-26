"""Dashboard reads only approved fields; public demo never reads a real portfolio."""

import json
from pathlib import Path
import sqlite3

from sequoia_x.reporting.dashboard import dataset, export_dashboard, demo_dataset
from sequoia_x.reporting.paper import PaperBook


def test_empty_dashboard_does_not_initialize_or_modify_a_ledger(tmp_path):
    root = tmp_path / "empty"
    data = dataset(root)
    assert not root.exists()
    assert data["markets"]["A"]["currency"] == "CNY"
    assert data["markets"]["US"]["currency"] == "USD"
    assert data["markets"]["A"]["history"] == []


def fixture_report(root, name="微软"):
    path = root / "paper/daily/2026-10-23-US"
    path.mkdir(parents=True)
    obj = {"demo": False, "private_secret": "SHOULD_NOT_EXPORT", "markets": [{
        "market": "US", "as_of": "2026-10-23", "pool_size": 1, "items": [{
            "symbol": "MSFT", "name": name, "status": "待核验", "valuation": {"status": "待核验",
            "private_notes": "SHOULD_NOT_EXPORT"}, "raw_price": 100, "credential": "SHOULD_NOT_EXPORT"}],
        "candidate_count": 0, "complete": False}]}
    (path / "report.json").write_text(json.dumps(obj))
    return path


def test_whitelist_never_exports_secret_files_or_internal_fields(tmp_path):
    fixture_report(tmp_path)
    (tmp_path / ".env").write_text("FEISHU_REPORT_WEBHOOK_URL=SHOULD_NOT_EXPORT")
    data = dataset(tmp_path)
    raw = json.dumps(data)
    assert "SHOULD_NOT_EXPORT" not in raw
    assert "MSFT" in raw and data["markets"]["US"]["candidate_count"] == 0


def test_export_escapes_script_termination_and_is_self_contained(tmp_path):
    fixture_report(tmp_path, name="</script><script>alert('xss')</script>")
    result = export_dashboard(tmp_path, tmp_path / "dashboard")
    text = result.read_text()
    assert "<script>alert('xss')</script>" not in text
    assert "\\u003c/script\\u003e" in text
    assert "__CSS__" not in text and "__JS__" not in text and "__DATA__" not in text
    assert "connect-src 'none'" in text and 'script src=' not in text


def test_demo_has_only_synthetic_symbols_and_never_reads_real_files(tmp_path):
    fixture_report(tmp_path)
    p = export_dashboard(tmp_path, tmp_path / "demo", demo=True)
    data = json.loads(p.with_name("dashboard.json").read_text())
    assert data["demo"] and "MSFT" not in json.dumps(data)
    assert all(x["symbol"].startswith("DEMO_") for m in data["markets"].values() for x in m["observations"])


def test_export_does_not_mutate_account_and_keeps_currencies_separate(tmp_path):
    path = tmp_path / "_state/paper.sqlite"
    book = PaperBook(path)
    with book.connect() as c:
        c.execute("INSERT INTO snapshots VALUES ('A','2026-10-23','1100','1000','0','[]',1)")
        c.execute("INSERT INTO snapshots VALUES ('US','2026-10-23',NULL,'2000','0','[]',0)")
    before = path.read_bytes()
    data = dataset(tmp_path)
    assert path.read_bytes() == before
    assert data["markets"]["A"]["history"][0]["pnl"] == 100
    assert data["markets"]["US"]["history"][0]["pnl"] is None
    assert data["markets"]["US"]["history"][0]["return"] is None


def test_demo_deployment_workflow_cannot_read_or_publish_real_portfolio():
    text = Path('.github/workflows/dashboard-demo.yml').read_text()
    assert '--demo --output _site' in text
    assert 'workflow_dispatch' in text and 'schedule:' not in text
    assert 'FEISHU' not in text and 'paper.sqlite' not in text
