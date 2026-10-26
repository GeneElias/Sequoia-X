"""One-shot Actions entry; no background server, safe encrypted delivery checkpoints."""

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .github_state import GitHubState, StateError
from .simulation import Simulation


TASKS = {"0 9 * * 1-5": "daily_A", "30 23 * * 1-5": "daily_US", "0 1 * * 6": "weekly"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GitHub Actions encrypted-state runner")
    parser.add_argument("--task", choices=["selftest", "validate", "daily_A", "daily_US", "weekly"], default=None)
    parser.add_argument("--root", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    task = args.task or TASKS.get(os.environ.get("SEQUOIA_CRON", ""))
    state = None
    try:
        if task is None:
            raise StateError("无法识别任务，不猜测执行范围")
        if task == "selftest":
            # Synthetic fixtures only; no real state, credentials or messages required.
            code = ("from unittest.mock import patch\nimport pytest\n"
                    "with patch('sequoia_x.notify.feishu.FeishuNotifier._get_stock_names', "
                    "side_effect=lambda codes: {code: code for code in codes}):\n"
                    "    raise SystemExit(pytest.main(['-q']))\n")
            return subprocess.run([sys.executable, "-c", code]).returncode
        key = os.environ.get("SEQUOIA_STATE_KEY", "").encode()
        if not key:
            raise StateError("缺少SEQUOIA_STATE_KEY")
        state = GitHubState(Path.cwd(), args.root, key)
        state.restore()
        if task == "validate":
            print(json.dumps({"task": task, "state": "decrypted_and_integrity_checked", "sent": False,
                              "schedule_paused": os.environ.get("SEQUOIA_SCHEDULE_PAUSED") == "true"}))
            return 0
        if not os.environ.get("FEISHU_REPORT_WEBHOOK_URL"):
            raise StateError("缺少飞书机器人配置，尚未处理模拟订单")
        # The checkpoint commits pending before HTTP and sent/unknown after HTTP.
        # A cancelled runner resumes with an unknown/pending record, not a duplicate send.
        sim = Simulation(args.root, args.root / "_state" / "valuations.json", send=False,
                         checkpoint=state.save)
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            if task == "weekly":
                result = sim.weekly()
                artifact = Path(result["weekly"]).with_name("cards.json")
            else:
                result = sim.daily(task.removeprefix("daily_"))
                artifact = Path(result["daily"][0]["report"]).with_name("cards.json")
            state.save()
            sim.send = True
            delivered = sim.deliver(json.loads(artifact.read_text()))
            state.save()
        print(json.dumps({"task": task, "state": "persisted_encrypted", "delivery": delivered}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Only persist if restoration was completed. Never overwrite state after decrypt errors.
        if state and state.restored:
            try:
                state.save()
            except Exception:
                print("State persistence failed; do not reset the ledger", file=sys.stderr)
        print(f"Actions任务失败（{type(exc).__name__}）；原始异常已隐藏，请检查权限、Secret及加密状态", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
