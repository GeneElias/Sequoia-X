"""Shanghai deadlines and durable per-session receipts for delayed GitHub cron."""

from datetime import datetime, time, timedelta
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from .delivery import with_dashboard
from .market import latest_session
from .report import content_id

SHANGHAI = ZoneInfo("Asia/Shanghai")


def task_info(task, now):
    if task == "weekly":
        local = now.astimezone(SHANGHAI)
        saturday = local.date() - timedelta(days=(local.weekday() - 5) % 7)
        deadline = datetime.combine(saturday, time(9), SHANGHAI)
        if local < deadline:
            saturday -= timedelta(days=7)
            deadline = datetime.combine(saturday, time(9), SHANGHAI)
        day = str(saturday - timedelta(days=1))
        return f"weekly:{day}", deadline, day
    market = task.removeprefix("daily_")
    day = latest_session(market, now)
    target = datetime.fromisoformat(day).date() + timedelta(days=market == "US")
    deadline = datetime.combine(target, time(7, 30) if market == "US" else time(17), SHANGHAI)
    return f"{task}:{day}", deadline, day


def due_tasks(now):
    return [t for t in ("daily_US", "daily_A", "weekly") if now >= task_info(t, now)[1]]


def artifact_path(root, task, day):
    return (
        root / "paper/weekly" / day / "cards.json"
        if task == "weekly"
        else root / "paper/daily" / f"{day}-{task.removeprefix('daily_')}" / "cards.json"
    )


def all_sent(root, cards, webhook, dashboard_url):
    path = root / "_state/delivery.sqlite"
    if not path.exists():
        return False
    destination = hashlib.sha256(webhook.encode()).hexdigest()
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        decorated = with_dashboard(cards, dashboard_url)
        for original, card in zip(cards, decorated):
            variants = [card, original]
            if dashboard_url:
                legacy = json.loads(json.dumps(card))
                legacy["card"]["elements"][-1]["actions"][0]["text"]["content"] = (
                    "打开看板（公开演示）"
                )
                variants.append(legacy)
            rows = [
                conn.execute(
                    "SELECT state FROM deliveries WHERE id=?", (destination + ":" + content_id(v),)
                ).fetchone()
                for v in variants
            ]
            if not any(row and row[0] == "sent" for row in rows):
                return False
    return bool(cards)


def read_operations(root):
    p = root / "_state/operations.json"
    return json.loads(p.read_text()) if p.exists() else {}


def write_operations(root, operations):
    from .simulation import write_json

    write_json(root / "_state/operations.json", operations)
