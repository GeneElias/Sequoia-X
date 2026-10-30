"""Live read-only health checks; private evidence stays in the encrypted branch."""

import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import requests

from .fundamentals import Fundamentals
from .github_state import GitHubState
from .market import (
    MarketData,
    MarketDataError,
    VALIDATION_REASONS,
    latest_session,
    normalize,
    validate_window,
)
from .report import DEFAULT_UNIVERSE, build_report


def action_timings(repository, token, get=requests.get):
    """Record creation, runner start and step durations, without request headers."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    base = "https://api.github.com/repos/" + repository
    response = get(
        base + "/actions/workflows/research-schedule.yml/runs",
        params={"per_page": 12},
        headers=headers,
        timeout=15,
    )
    result = {"status": response.status_code, "runs": []}
    if not response.ok:
        return result
    for run in response.json().get("workflow_runs", []):
        row = {
            k: run.get(k)
            for k in [
                "id",
                "event",
                "status",
                "conclusion",
                "created_at",
                "run_started_at",
                "updated_at",
            ]
        }
        jobs = get(base + "/actions/runs/" + str(run["id"]) + "/jobs", headers=headers, timeout=15)
        if jobs.ok:
            row["jobs"] = []
            for job in jobs.json().get("jobs", []):
                row["jobs"].append(
                    {
                        **{k: job.get(k) for k in ["started_at", "completed_at", "conclusion"]},
                        "steps": [
                            {
                                k: s.get(k)
                                for k in ["name", "started_at", "completed_at", "conclusion"]
                            }
                            for s in job.get("steps", [])
                        ],
                    }
                )
        result["runs"].append(row)
    return result


def preview(root, now, force=True):
    """Neither Simulation nor PaperBook is instantiated: no orders or receipts change."""
    provider = MarketData(root / "_state/cache", refresh=True)
    reports = []
    for market in ("A", "US"):
        as_of = latest_session(market, now)
        inputs = Fundamentals(root / "_state/fundamentals.json").refresh(
            market, DEFAULT_UNIVERSE[market], as_of, force=force
        )
        reports.append(build_report({market: DEFAULT_UNIVERSE[market]}, provider, inputs, now=now))
    return reports


def probe(root, now):
    provider = MarketData(root / "_state/cache")
    result = []
    as_of = latest_session("A", now)
    for symbol in ["600519", "000858", "300750"]:
        for method in ["_akshare", "_tencent", "_yahoo"]:
            row = {"symbol": symbol, "adapter": method}
            try:
                h = getattr(provider, method)(symbol, "A", as_of)
                h.frame = normalize(h.frame, as_of)
                validate_window(h.frame, "A", as_of)
                row.update(status="ok", last=h.frame.index[-1], length=len(h.frame))
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, MarketDataError)
                    or (isinstance(exc, ValueError) and str(exc) in VALIDATION_REASONS)
                    else type(exc).__name__
                )
                row.update(status="failed", reason=reason)
            result.append(row)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Encrypted read-only diagnostics; never sends or trades"
    )
    parser.add_argument(
        "--full", action="store_true", help="Fetch and reconcile both 25-stock pools"
    )
    parser.add_argument("--probe", action="store_true", help="Probe individual A-share adapters")
    args = parser.parse_args(argv)
    root = Path("reports")
    state = GitHubState(Path.cwd(), root, os.environ["SEQUOIA_STATE_KEY"].encode())
    state.restore()
    now = datetime.now(timezone.utc)
    result = {
        "generated_at": now.isoformat(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "read_only_ledger": True,
    }
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        if args.full:
            result["repair_preview"] = preview(root, now)
        if args.probe:
            result["quotes"] = probe(root, now)
        try:
            result["actions"] = action_timings(
                os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
            )
        except Exception as exc:
            result["actions"] = {"error": type(exc).__name__}
        (root / "_state/diagnostics.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        )
        state.save()
    print("Read-only diagnostics persisted encrypted; no orders or messages created.")
    # Financial blocks are legitimate outcomes, but an incomplete quote pool fails health.
    return (
        0
        if all(g["complete"] for r in result.get("repair_preview", []) for g in r["markets"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
