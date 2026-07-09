"""
Check open journal entries against real price action and print the
current forward-tested track record.

Usage: python journal_check.py
Run this periodically (e.g. daily) - separately from main.py's scanning -
so open setups get resolved once their stop/target/horizon plays out.
"""

from pathlib import Path

import yaml

from scanner.journal import check_open_entries, summarize, JOURNAL_PATH

CONFIG_PATH = Path(__file__).parent / "config" / "settings.yaml"


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    journal_cfg = cfg.get("journal", {})
    horizon = journal_cfg.get("horizon_candles", 20)

    if not JOURNAL_PATH.exists():
        print("No journal.jsonl yet - run main.py first to log some setups.")
        return

    updated = check_open_entries(horizon_candles=horizon)
    print(f"Resolved {updated} entr{'y' if updated == 1 else 'ies'}.\n")

    summary = summarize()
    if not summary:
        print("No resolved entries yet.")
        return

    hdr = f"{'detector':<28}{'n':>5}{'win_rate':>10}{'avg%':>8}{'expired':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name, s in summary.items():
        wr = f"{s['win_rate']:.1%}" if s["win_rate"] is not None else "n/a"
        print(f"{name:<28}{s['n']:>5}{wr:>10}{s['avg_outcome_pct']:>8.2f}{s['expired']:>9}")


if __name__ == "__main__":
    main()
