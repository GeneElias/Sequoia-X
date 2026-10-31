"""Read-only incident probe; evidence is kept in encrypted state only."""

import contextlib
from datetime import datetime, timezone
import json, os
from pathlib import Path
import pandas as pd
from .github_state import GitHubState
from .market import MarketData, latest_session, normalize, validate_window, VALIDATION_REASONS


def main():
    root = Path("reports")
    state = GitHubState(Path.cwd(), root, os.environ["SEQUOIA_STATE_KEY"].encode())
    state.restore()
    now = datetime.now(timezone.utc)
    as_of = latest_session("US", now)
    result = {
        "kind": "us_morning_incident",
        "generated_at": now.isoformat(),
        "as_of": as_of,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "probes": [],
    }
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        import yfinance as yf

        for symbol in ["MSFT", "NVDA", "GOOGL", "COST"]:
            row = {"symbol": symbol}
            try:
                raw = yf.Ticker(symbol).history(period="2y", auto_adjust=False, timeout=12)
                row["raw_columns"] = list(raw.columns)
                row["rows"] = len(raw)
                row["raw_tail"] = json.loads(
                    raw.reset_index().tail(4).to_json(orient="records", date_format="iso")
                )
                row["missing"] = {
                    str(c): raw.index[raw[c].isna()].astype(str).tolist()[-12:]
                    for c in raw.columns
                    if raw[c].isna().any()
                }
                h = MarketData._yahoo(symbol, "US", as_of)
                row["normalized_missing"] = {
                    str(c): h.frame.index[h.frame[c].isna()].astype(str).tolist()[-12:]
                    for c in h.frame.columns
                    if h.frame[c].isna().any()
                }
                try:
                    h.frame = normalize(h.frame, as_of)
                    validate_window(h.frame, "US", as_of)
                    row["status"] = "ok"
                except ValueError as exc:
                    row["status"] = (
                        str(exc) if str(exc) in VALIDATION_REASONS else "validation_error"
                    )
                short = yf.Ticker(symbol).history(period="6mo", auto_adjust=False, timeout=12)
                row["short_tail"] = json.loads(
                    short.reset_index().tail(3).to_json(orient="records", date_format="iso")
                )
            except Exception as exc:
                row["error"] = type(exc).__name__
            result["probes"].append(row)
        (root / "_state/diagnostics.json").write_text(
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        )
        state.save()
    print("Incident probe persisted encrypted; no orders or messages created.")


if __name__ == "__main__":
    main()
