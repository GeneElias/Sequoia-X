"""One-shot Actions runner with encrypted checkpoints and delayed-cron recovery."""

import argparse
import contextlib
from datetime import datetime, timedelta, timezone
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


def data_complete(root, task, day):
    """Separate report/data health from HTTP delivery success."""
    path = (
        root / "paper/weekly" / day / "review.json"
        if task == "weekly"
        else root / "paper/daily" / f"{day}-{task.removeprefix('daily_')}" / "report.json"
    )
    if not path.exists():
        return None  # Legacy receipt-only fixtures; real reports retain their JSON.
    report = json.loads(path.read_text())
    groups = report.get("markets", [])
    complete = bool(groups) and all(g.get("complete") is True for g in groups)
    paper = path.with_name("paper.json")
    if task != "weekly" and paper.exists():
        complete = complete and json.loads(paper.read_text()).get("complete") is True
    return complete


def retry_record(previous, now, deadline):
    attempts = int(previous.get("data_retry_count", 0)) + 1
    minutes = min(180, 30 * 2 ** min(attempts - 1, 3))
    return {
        **previous,
        "phase": "awaiting_data",
        "bad_notice_sent": True,
        "data_retry_count": attempts,
        "last_attempt_at": now.isoformat(),
        "planned_at": deadline.isoformat(),
        "next_retry_at": (now + timedelta(minutes=minutes)).isoformat(),
    }


def prepare_cards(artifact, now, deadline, recovered=False, incomplete=False):
    cards = json.loads(artifact.read_text())
    lag = max(0, int((now - deadline).total_seconds() / 60))
    if recovered:
        note = "数据恢复更新：此前该期报告因行情不完整暂停排名；本次已取得完整有效数据，请以本次更新为准。\n"
        for card in cards:
            title = card.get("card", {}).get("header", {}).get("title")
            if title:
                title["content"] = "数据已恢复｜" + title["content"]
    elif incomplete:
        note = "数据异常通知：本期行情或持仓估值尚未完整，不能解释为没有机会或零收益。系统保留异常并自动退避重试；恢复后更新该期报告，不补买错过的开盘。\n"
    else:
        note = ""
    note += f"计划时间 {deadline:%Y-%m-%d %H:%M}（北京时间）；本次开始 {now.astimezone(deadline.tzinfo):%H:%M}，晚于计划 {lag} 分钟。GitHub 定时任务不保证准点；完整任务完成后补检不重发。"
    element = {"tag": "div", "text": {"tag": "lark_md", "content": note}}
    cards[-1]["card"]["elements"].append(element)
    if len(json.dumps(cards[-1], ensure_ascii=False).encode()) > 18000:
        cards[-1]["card"]["elements"].pop()
        cards.append({"msg_type": "interactive", "card": {"elements": [element]}})
    write_json(artifact, cards)
    return lag


def execute(state, task, now=None):
    """Retry unhealthy data with backoff; preserve exact in-flight card hashes."""
    controlled_clock = now is not None
    now = now or datetime.now(timezone.utc)
    root = state.root
    webhook = os.environ.get("FEISHU_REPORT_WEBHOOK_URL", "")
    if not webhook:
        raise StateError("缺少飞书机器人配置，尚未处理模拟订单")
    operations = read_operations(root)
    identity, deadline, day = task_info(task, now)
    record = operations.get(identity, {})
    artifact = artifact_path(root, task, day)
    complete = data_complete(root, task, day)
    if record.get("phase") == "done" and complete is not False:
        return {"task": task, "state": "already_completed"}
    if record.get("phase") == "prepared" and not artifact.exists():
        raise StateError("准备发送的卡片缺失，拒绝重新生成绕过未知发送记录")
    sent = artifact.exists() and all_sent(
        root, json.loads(artifact.read_text()), webhook, os.environ.get("FEISHU_DASHBOARD_URL")
    )
    if (
        sent
        and complete is not False
        and record.get("phase") not in {"awaiting_data", "generating"}
    ):
        operations[identity] = {
            "phase": "done",
            "completed_at": now.isoformat(),
            "adopted_receipt": True,
        }
        write_operations(root, operations)
        state.save()
        return {"task": task, "state": "already_delivered"}
    notice_sent = bool(record.get("bad_notice_sent")) or bool(sent and complete is False)
    if record.get("phase") == "awaiting_data":
        next_retry = record.get("next_retry_at")
        if next_retry and now < datetime.fromisoformat(next_retry):
            return {"task": task, "state": "retry_scheduled", "next_retry_at": next_retry}
    # In-flight cards must be resolved first. Only confirmed old bad reports or
    # unsent recheck previews may be regenerated; pending/unknown are never bypassed.
    regenerate = (
        not artifact.exists()
        or record.get("phase") in {"awaiting_data", "generating"}
        or (complete is False and sent)
    )
    sim = Simulation(root, root / "_state/valuations.json", send=False, checkpoint=state.save)
    started = time.monotonic()
    if regenerate:
        record = {
            k: v
            for k, v in record.items()
            if k
            not in {"adopted_receipt", "completed_at", "duration_seconds", "resumed_existing_cards"}
        }
        # Persist intent before producing a multi-file report. A failed generation
        # must not pair a new report.json with an old already-sent cards.json.
        operations[identity] = {
            **record,
            "phase": "generating",
            "bad_notice_sent": notice_sent,
            "generation_started_at": now.isoformat(),
        }
        write_operations(root, operations)
        state.save()
        if task == "weekly":
            sim.weekly(week_end=day, now=now if controlled_clock else None)
        else:
            sim.daily(task.removeprefix("daily_"), now=now if controlled_clock else None)
        complete = data_complete(root, task, day)
        if complete is False and notice_sent:
            operations[identity] = retry_record(record, now, deadline)
            write_operations(root, operations)
            state.save()
            return {
                "task": task,
                "state": "awaiting_data",
                "sent": False,
                "next_retry_at": operations[identity]["next_retry_at"],
            }
        lag = prepare_cards(
            artifact,
            now,
            deadline,
            recovered=notice_sent and complete is True,
            incomplete=complete is False,
        )
        operations[identity] = {
            **record,
            "phase": "prepared",
            "planned_at": deadline.isoformat(),
            "started_at": now.isoformat(),
            "lag_minutes": lag,
            "bad_notice_sent": notice_sent,
            "data_complete": complete,
        }
        write_operations(root, operations)
        state.save()
    elif record.get("phase") != "prepared":
        operations[identity] = {
            **record,
            "phase": "prepared",
            "resumed_existing_cards": True,
            "bad_notice_sent": notice_sent,
            "data_complete": complete,
        }
        write_operations(root, operations)
        state.save()
    sim.send = True
    delivered = sim.deliver(json.loads(artifact.read_text()))
    if delivered.get("status") != "attempted":
        raise StateError("飞书发送未执行，不标记任务完成")
    if complete is False:
        operations[identity] = retry_record(operations[identity], now, deadline)
        result = {
            "task": task,
            "state": "awaiting_data",
            "delivery": delivered,
            "next_retry_at": operations[identity]["next_retry_at"],
        }
    else:
        operations[identity].update(
            phase="done",
            completed_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(time.monotonic() - started, 2),
        )
        result = {"task": task, "state": "persisted_encrypted", "delivery": delivered}
    write_operations(root, operations)
    state.save()
    return result


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
        return 1 if any(r["state"] in {"delivery_blocked", "awaiting_data"} for r in results) else 0
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
