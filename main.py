"""
Binance Multi-Timeframe Pattern Scanner
========================================
Run:  python main.py
No API keys needed - public market data only.

Output: console report + signals_output.json
(the JSON is what you later feed to Claude/Gemini for synthesis,
or to a Telegram notifier.)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scanner.data import get_klines, get_all_usdt_pairs
from scanner.indicators import enrich
from scanner.patterns import run_all_detectors
from scanner.mtf import annotate_htf
from scanner.notify import notify_report
from scanner.risk import attach_atr_risk, setup_risk_plan
from scanner.journal import log_signals

CONFIG_PATH = Path(__file__).parent / "config" / "settings.yaml"
BACKTEST_RESULTS_PATH = Path(__file__).parent / "backtest_results.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_detector_weights(cfg: dict) -> dict:
    """
    Turn backtest_results.json into a {(detector, direction): weight} map so
    strength reflects each detector's actual backtested edge instead of
    counting every signal equally. weight = 1.0 + scale * (detector's win
    rate - baseline win rate for that direction), clamped to a sane range.
    Falls back to an empty map (neutral weight 1.0 everywhere) if the
    backtest hasn't been run yet or weighting is turned off.
    """
    conf_cfg = cfg.get("confluence", {})
    if not conf_cfg.get("use_backtest_weights", True) or not BACKTEST_RESULTS_PATH.exists():
        return {}
    summary = json.loads(BACKTEST_RESULTS_PATH.read_text()).get("summary", {})
    horizon = conf_cfg.get("horizon", "h10")
    min_signals = conf_cfg.get("min_signals", 30)
    scale = conf_cfg.get("weight_scale", 5.0)
    lo, hi = conf_cfg.get("weight_clamp", [0.2, 2.5])

    baseline = {}
    for direction in ("bullish", "bearish"):
        entry = summary.get(f"ALL/{direction}", {})
        if horizon in entry:
            baseline[direction] = entry[horizon]["win_rate"]

    weights = {}
    for key, entry in summary.items():
        if key.startswith("ALL/") or horizon not in entry:
            continue
        detector, _, direction = key.rpartition("/")
        if direction not in baseline or entry["signals"] < min_signals:
            continue
        edge = entry[horizon]["win_rate"] - baseline[direction]
        weights[(detector, direction)] = max(lo, min(hi, 1.0 + scale * edge))
    return weights


def confluence_score(signals: list[dict], weights: dict | None = None) -> tuple[str, float]:
    """Net directional bias and strength from a signal list, weighted by backtested edge."""
    weights = weights or {}
    bull = sum(weights.get((s["name"], "bullish"), 1.0) for s in signals if s["direction"] == "bullish")
    bear = sum(weights.get((s["name"], "bearish"), 1.0) for s in signals if s["direction"] == "bearish")
    if bull > bear:
        return "bullish", round(bull, 2)
    if bear > bull:
        return "bearish", round(bear, 2)
    return "mixed", round(max(bull, bear), 2)


def scan_pair(symbol: str, timeframes: list[str], cfg: dict, weights: dict) -> dict:
    result = {"symbol": symbol, "timeframes": {}}
    for tf in timeframes:
        df = get_klines(symbol, tf, cfg["candle_limit"])
        if df is None or len(df) < 60:
            if df is None:
                print(f"  [skip] {symbol} {tf}: no data (bad symbol or API error)")
            continue
        df = enrich(df, cfg)
        signals = run_all_detectors(df, cfg)
        if signals:
            close = float(df["close"].iloc[-1])
            atr = float(df["atr"].iloc[-1])
            risk_cfg = cfg.get("risk", {})
            signals = attach_atr_risk(signals, close, atr,
                                      risk_cfg.get("atr_multiplier", 1.5),
                                      risk_cfg.get("reward_risk_ratio", 2.0),
                                      risk_cfg.get("max_stop_pct"))
            bias, strength = confluence_score(signals, weights)
            result["timeframes"][tf] = {
                "close": close,
                "bias": bias,
                "strength": strength,
                "signals": signals,
                "risk": setup_risk_plan(signals, bias, close),
            }
        time.sleep(0.15)  # be polite to the API
    return result


def main():
    cfg = load_config()
    pairs = get_all_usdt_pairs() if cfg["scan_all"] else cfg["pairs"]
    timeframes = cfg["timeframes"]
    min_conf = cfg["output"]["min_confluence"]
    weights = load_detector_weights(cfg)

    print(f"Scanning {len(pairs)} pairs x {len(timeframes)} timeframes...")
    if weights:
        print(f"(strength weighted by backtested edge - {len(weights)} detector/direction weights loaded)\n")
    else:
        print("(no backtest_results.json found - strength is unweighted; run backtest.py to enable weighting)\n")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": []}

    for symbol in pairs:
        try:
            res = scan_pair(symbol, timeframes, cfg, weights)
        except Exception as e:
            print(f"  [error] {symbol}: {e}")
            continue
        if not res["timeframes"]:
            continue
        res = annotate_htf(res, timeframes)
        res["max_strength"] = max(d["strength"] for d in res["timeframes"].values())
        report["results"].append(res)

        # console output for setups meeting min confluence
        for tf, data in res["timeframes"].items():
            if data["strength"] >= min_conf:
                if cfg.get("mtf", {}).get("require_agreement", False) and not data["htf_agrees"]:
                    continue
                print(f"{symbol} [{tf}]  {data['bias'].upper()} (strength {data['strength']})  close={data['close']:.6g}  [HTF: {data['htf_note']}]")
                for s in data["signals"]:
                    print(f"    - {s['name']}: {s['detail']}")
                if data["risk"]:
                    r = data["risk"]
                    rr = f"{r['risk_reward']}:1" if r["risk_reward"] else "n/a"
                    print(f"    risk: entry={r['entry']:.6g} stop={r['stop']:.6g} "
                          f"target={r['target']:.6g} (R:R {rr}, based on {r['based_on']})")
                print()

    report["results"].sort(key=lambda r: r["max_strength"], reverse=True)

    if cfg["output"]["mode"] in ("json", "both"):
        out = Path(__file__).parent / cfg["output"]["json_path"]
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nFull report written to {out}")

    sent = notify_report(report, cfg)
    if sent:
        print(f"Sent {sent} Telegram alert(s)")

    if cfg.get("journal", {}).get("enabled", True):
        logged = log_signals(report, cfg)
        if logged:
            print(f"Logged {logged} new setup(s) to journal.jsonl")


if __name__ == "__main__":
    main()
