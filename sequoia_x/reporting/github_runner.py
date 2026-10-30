"""One-shot Actions runner with encrypted checkpoints and delayed-cron recovery."""

import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .github_state import GitHubState, StateError
from .delivery import DeliveryError
from .scheduling import (
    all_sent,
    artifact_path,
    due_tasks,
    read_operations,
    task_info,
    write_operations,
)
from .simulation import Simulation, write_json

TASKS = {
    "0 9 * * 1-5": "daily_A",
    "30 23 * * 1-5": "daily_US",
    "0 1 * * 6": "weekly",
    "13,43 * * * *": "due",
}


def execute(state, task, now=None):
    """Resume saved cards after interruptions; never rebuild or resend completed days."""
    controlled_clock = now is not None
    now = now or datetime.now(timezone.utc)
    root = state.root
    webhook = os.environ.get("FEISHU_REPORT_WEBHOOK_URL", "")
    if not webhook:
        raise StateError("缺少飞书机器人配置，尚未处理模拟订单")
    operations = read_operations(root)
    identity, deadline, day = task_info(task, now)
    record = operations.get(identity, {})
    if record.get("phase") == "done":
        return {"task": task, "state": "already_completed"}
    artifact = artifact_path(root, task, day)
    # Adopt receipts from before this change, including a crash after HTTP success.
    if artifact.exists() and all_sent(
        root, json.loads(artifact.read_text()), webhook, os.environ.get("FEISHU_DASHBOARD_URL")
    ):
        operations[identity] = {
            "phase": "done",
            "completed_at": now.isoformat(),
            "adopted_receipt": True,
        }
        write_operations(root, operations)
        state.save()
        return {"task": task, "state": "already_delivered"}
    sim = Simulation(root, root / "_state/valuations.json", send=False, checkpoint=state.save)
    started = time.monotonic()
    if not artifact.exists():
        if task == "weekly":
            sim.weekly(week_end=day, now=now if controlled_clock else None)
        else:
            sim.daily(task.removeprefix("daily_"), now=now if controlled_clock else None)
        cards = json.loads(artifact.read_text())
        lag = max(0, int((now - deadline).total_seconds() / 60))
        note = f"计划时间 {deadline:%Y-%m-%d %H:%M}（北京时间）；本次开始 {now.astimezone(deadline.tzinfo):%H:%M}，晚于计划 {lag} 分钟。GitHub 定时任务不保证准点；同一交易日完成后补检不重发。"
        cards[-1]["card"]["elements"].append(
            {"tag": "div", "text": {"tag": "lark_md", "content": note}}
        )
        # Match the delivery layer's byte budget, split timing into its own card if needed.
        if len(json.dumps(cards[-1], ensure_ascii=False).encode()) > 18000:
            cards[-1]["card"]["elements"].pop()
            cards.append(
                {
                    "msg_type": "interactive",
                    "card": {
                        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": note}}]
                    },
                }
            )
        write_json(artifact, cards)
        operations[identity] = {
            "phase": "prepared",
            "planned_at": deadline.isoformat(),
            "started_at": now.isoformat(),
            "lag_minutes": lag,
        }
        write_operations(root, operations)
        state.save()
    elif record.get("phase") != "prepared":
        # Existing unsent/unknown cards retain their exact content hash. Rebuilding
        # them could bypass the pending/unknown deduplication barrier.
        operations[identity] = {"phase": "prepared", "resumed_existing_cards": True}
        write_operations(root, operations)
        state.save()
    sim.send = True
    delivered = sim.deliver(json.loads(artifact.read_text()))
    if delivered.get("status") != "attempted":
        raise StateError("飞书发送未执行，不标记任务完成")
    operations[identity].update(
        phase="done",
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_seconds=round(time.monotonic() - started, 2),
    )
    write_operations(root, operations)
    state.save()
    return {"task": task, "state": "persisted_encrypted", "delivery": delivered}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Encrypted-state research runner")
    parser.add_argument(
        "--task", choices=["selftest", "validate", "daily_A", "daily_US", "weekly", "due"]
    )
    parser.add_argument("--root", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    task = args.task or TASKS.get(os.environ.get("SEQUOIA_CRON", ""))
    state = None
    try:
        if task is None:
            raise StateError("无法识别任务")
        if task == "selftest":
            code = (
                "from unittest.mock import patch\nimport pytest\n"
                "with patch('sequoia_x.notify.feishu.FeishuNotifier._get_stock_names', side_effect=lambda codes: {code: code for code in codes}):\n"
                "    raise SystemExit(pytest.main(['-q']))\n"
            )
            return subprocess.run([sys.executable, "-c", code]).returncode
        key = os.environ.get("SEQUOIA_STATE_KEY", "").encode()
        if not key:
            raise StateError("缺少SEQUOIA_STATE_KEY")
        state = GitHubState(Path.cwd(), args.root, key)
        state.restore()
        if task == "validate":
            print(
                json.dumps(
                    {
                        "task": task,
                        "state": "decrypted_and_integrity_checked",
                        "sent": False,
                        "schedule_paused": os.environ.get("SEQUOIA_SCHEDULE_PAUSED") == "true",
                    }
                )
            )
            return 0
        tasks = due_tasks(datetime.now(timezone.utc)) if task == "due" else [task]
        results = []
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            for item in tasks:
                try:
                    results.append(execute(state, item))
                except DeliveryError:
                    # An unresolved US delivery must not block the A-share/weekly job.
                    state.save()
                    results.append(
                        {
                            "task": item,
                            "state": "delivery_blocked",
                            "retry": "inspect_existing_receipt",
                        }
                    )
        print(json.dumps({"tasks": results}, ensure_ascii=False))
        return 1 if any(r["state"] == "delivery_blocked" for r in results) else 0
    except Exception as exc:
        if state and state.restored:
            try:
                state.save()
            except Exception:
                print("State persistence failed; do not reset the ledger", file=sys.stderr)
        print(
            f"Actions任务失败（{type(exc).__name__}）；敏感异常已隐藏，保留账本与发送状态",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
