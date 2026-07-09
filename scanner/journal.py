"""
Live signal journal - a forward-tested track record.

Backtests are retrospective and can be (even unintentionally) overfit to
the exact data they were tuned on. This journal logs every real setup the
scanner surfaces, then later checks what price actually did, building up
real forward performance over time - the only thing that actually proves
a system works going forward.

Storage: append-only JSONL (one JSON object per line) at journal.jsonl.
No database dependency, diff-friendly, trivial to append to from a
scheduled/cloud run.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from scanner.data import get_klines

JOURNAL_PATH = Path(__file__).resolve().parent.parent / "journal.jsonl"


def _load(path: Path = JOURNAL_PATH) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _save_all(entries: list[dict], path: Path = JOURNAL_PATH) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""))


def _append(entry: dict, path: Path = JOURNAL_PATH) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def log_signals(report: dict, cfg: dict, path: Path = JOURNAL_PATH) -> int:
    """
    Log every setup meeting min_confluence. Skips symbol/timeframe/pattern
    combos that already have an unresolved ("open") journal entry - without
    this, a persistent state signal (e.g. ema_stack, which fires on every
    run while the trend holds) would spam a fresh row every single scan
    instead of one row per real occurrence.
    """
    min_conf = cfg["output"]["min_confluence"]
    existing = _load(path)
    open_keys = {(e["symbol"], e["timeframe"], e["based_on"]) for e in existing if e["status"] == "open"}
    next_id = max((e["id"] for e in existing), default=0) + 1
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    logged = 0
    for res in report["results"]:
        for tf, data in res["timeframes"].items():
            if data["strength"] < min_conf or not data.get("risk"):
                continue
            r = data["risk"]
            key = (res["symbol"], tf, r["based_on"])
            if key in open_keys:
                continue
            _append({
                "id": next_id,
                "logged_at": now,
                "symbol": res["symbol"],
                "timeframe": tf,
                "bias": data["bias"],
                "strength": data["strength"],
                "entry": r["entry"],
                "stop": r["stop"],
                "target": r["target"],
                "based_on": r["based_on"],
                "signals": [s["name"] for s in data["signals"]],
                "status": "open",
                "checked_at": None,
                "outcome_price": None,
                "outcome_pct": None,
            }, path)
            open_keys.add(key)
            next_id += 1
            logged += 1
    return logged


def check_open_entries(path: Path = JOURNAL_PATH, horizon_candles: int = 20,
                       kline_limit: int = 500) -> int:
    """
    For every open entry, fetch candles since it was logged and see whether
    stop or target was hit first. If neither hits within horizon_candles,
    mark it "expired" and record what price actually did - a signal that
    never resolves either way is still useful information (it means the
    stop/target distances were too wide, or the setup just chopped).
    """
    entries = _load(path)
    if not entries:
        return 0
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    updated = 0

    for e in entries:
        if e["status"] != "open":
            continue
        df = get_klines(e["symbol"], e["timeframe"], kline_limit)
        if df is None:
            continue
        logged_at = datetime.fromisoformat(e["logged_at"])
        after = df[df["open_time"] > logged_at]
        if after.empty:
            continue

        outcome, outcome_price = None, None
        for _, candle in after.iterrows():
            if e["bias"] == "bullish":
                if candle["low"] <= e["stop"]:
                    outcome, outcome_price = "loss", e["stop"]
                    break
                if candle["high"] >= e["target"]:
                    outcome, outcome_price = "win", e["target"]
                    break
            else:
                if candle["high"] >= e["stop"]:
                    outcome, outcome_price = "loss", e["stop"]
                    break
                if candle["low"] <= e["target"]:
                    outcome, outcome_price = "win", e["target"]
                    break

        if outcome is None and len(after) >= horizon_candles:
            outcome, outcome_price = "expired", float(after["close"].iloc[-1])

        if outcome:
            e["status"] = outcome
            e["checked_at"] = now
            e["outcome_price"] = outcome_price
            e["outcome_pct"] = round((outcome_price - e["entry"]) / e["entry"] * 100, 3)
            updated += 1

    if updated:
        _save_all(entries, path)
    return updated


def summarize(path: Path = JOURNAL_PATH) -> dict:
    """Win rate and average return per detector, from resolved journal entries only."""
    entries = [e for e in _load(path) if e["status"] in ("win", "loss", "expired")]
    by_detector: dict[str, list[dict]] = {}
    for e in entries:
        by_detector.setdefault(e["based_on"], []).append(e)

    summary = {}
    for name, rows in sorted(by_detector.items()):
        wins = sum(1 for r in rows if r["status"] == "win")
        losses = sum(1 for r in rows if r["status"] == "loss")
        decided = wins + losses
        summary[name] = {
            "n": len(rows),
            "wins": wins,
            "losses": losses,
            "expired": len(rows) - decided,
            "win_rate": round(wins / decided, 3) if decided else None,
            "avg_outcome_pct": round(sum(r["outcome_pct"] for r in rows) / len(rows), 3),
        }
    return summary
