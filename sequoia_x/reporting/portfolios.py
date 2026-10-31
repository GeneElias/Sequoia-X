"""Separate forward-only portfolios; legacy ledger remains the strict value book."""

from datetime import datetime, timezone
import json
from pathlib import Path

from .market import latest_session
from .paper import PaperBook, next_session

LABELS = {"strict_value": "严格价值组合", "technical": "量价实验组合"}


def ensure_portfolios(
    root: Path, now: datetime | None = None, slippage_bps="5", fee_per_order="0"
) -> dict:
    now = now or datetime.now(timezone.utc)
    state = root / "_state"
    marker = state / "portfolios.json"
    ledger = state / "paper-technical.sqlite"
    if marker.exists():
        config = json.loads(marker.read_text())
        if config.get("schema") != 1 or not ledger.is_file():
            raise RuntimeError("双组合状态不完整，拒绝重建或重置账本")
        for market in ("A", "US"):
            datetime.fromisoformat(config["technical_first_signal"][market])
        return config
    # No orders/snapshots are created here. Adoption only starts with a NEW signal day.
    if ledger.exists():
        raise RuntimeError("发现无启用记录的量价账本，拒绝自动接管")
    PaperBook(ledger, slippage_bps, fee_per_order, portfolio="technical")
    config = {
        "schema": 1,
        "activated_at": now.isoformat(),
        "technical_first_signal": {
            market: next_session(market, latest_session(market, now)) for market in ("A", "US")
        },
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    temporary.replace(marker)
    return config
