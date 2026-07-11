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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scanner.data import get_klines, get_all_usdt_pairs, get_top_pairs_by_volume, get_current_price
from scanner.indicators import enrich
from scanner.patterns import run_all_detectors
from scanner.mtf import annotate_htf
from scanner.notify import notify_report
from scanner.risk import attach_atr_risk, setup_risk_plan
from scanner.journal import log_signals, detector_recent_form, mark_notified, detector_expectancy, detector_avg_return
from scanner.regime import regime_label

CONFIG_PATH = Path(__file__).parent / "config" / "settings.yaml"
BACKTEST_RESULTS_PATH = Path(__file__).parent / "backtest_results.json"
REALISTIC_BACKTEST_PATH = Path(__file__).parent / "realistic_backtest_results.json"


def load_realistic_backtest_expectancy(min_n: int) -> dict[tuple[str, str], tuple[float, int]]:
    """
    Real expectancy (in R) per (detector, direction) from
    realistic_backtest.py's historical simulation - the SAME stop/target-
    respecting methodology as the live journal (see
    journal.detector_expectancy), just at much larger scale (thousands of
    simulated trades vs. the live journal's slower-accumulating real ones).
    Returns (expectancy, n) - see detector_reliability_verdict for why the
    sample size matters here, not just the expectancy value.

    Expectancy, not raw win rate, is what actually determines whether a
    detector makes or loses money - a structural pattern with a wide,
    geometry-based target only needs a low win rate to be profitable (e.g.
    ascending_triangle/bullish: 29.3% win rate against a 14.96:1 average
    R:R is comfortably profitable, needing only ~6.3% to break even), while
    a 2:1 generic ATR-based detector needs ~33.3%. Comparing every detector
    against one flat win-rate bar would blacklist the system's best
    performers alongside its worst - this is what a flat threshold
    literally did on the first pass here before being caught and fixed.
    """
    if not REALISTIC_BACKTEST_PATH.exists():
        return {}
    summary = json.loads(REALISTIC_BACKTEST_PATH.read_text()).get("summary", {})
    out = {}
    for key, entry in summary.items():
        if entry["n"] < min_n:
            continue
        detector, _, direction = key.rpartition("/")
        out[(detector, direction)] = (entry["win_rate"] * (1 + entry["avg_risk_reward"]) - 1, entry["n"])
    return out


def combined_detector_expectancy(backtest: dict[tuple[str, str], tuple[float, int]],
                                 live: dict[tuple[str, str], tuple[float, int]]
                                 ) -> dict[tuple[str, str], float]:
    """
    Pool the two expectancy sources weighted by sample size, instead of
    letting the live journal simply override the backtest the moment it
    crosses min_n. A live-only override is wrong here: min_n=10 live trades
    is nowhere near enough to overturn a 300+ trade historical simulation,
    but that's exactly what a naive override does - this is what let
    volume_spike/bullish (backtest: -0.265R over 315 trades) get
    UN-blacklisted by just 10-15 live trades that happened to average
    +0.38R, pure small-sample noise given how far it diverges from the
    much larger sample. Weighting by n means a detector's verdict only
    flips once the live evidence is substantial enough to actually matter.
    """
    out = {}
    for key in set(backtest) | set(live):
        bt_exp, bt_n = backtest.get(key, (0.0, 0))
        live_exp, live_n = live.get(key, (0.0, 0))
        total_n = bt_n + live_n
        out[key] = (bt_exp * bt_n + live_exp * live_n) / total_n if total_n else 0.0
    return out


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


def get_market_trend(symbol: str, timeframes: list[str], cfg: dict) -> dict[str, bool | None]:
    """
    Current trend (close > EMA20) per timeframe for a market-leader symbol
    (BTC/ETH) - True=bullish, False=bearish, None if data unavailable.
    Computed once per run and shared across every pair's scan (see
    risk.setup_risk_plan's market_disagrees param for why this matters).
    """
    trend = {}
    for tf in timeframes:
        df = get_klines(symbol, tf, cfg["candle_limit"])
        if df is None or len(df) < 25:
            trend[tf] = None
            continue
        ema20 = df["close"].ewm(span=20, adjust=False).mean()
        trend[tf] = bool(df["close"].iloc[-1] > ema20.iloc[-1])
    return trend


def scan_pair(symbol: str, timeframes: list[str], cfg: dict, weights: dict,
             avg_returns: dict | None = None, unreliable: set | None = None,
             btc_trend: dict | None = None, eth_trend: dict | None = None) -> dict:
    result = {"symbol": symbol, "timeframes": {}}
    # One live-price call shared across all timeframes for this symbol - the
    # closed candle's close can be up to a full candle-period stale (up to
    # 24h on 1d), so alerts quote this instead for entry/display, while
    # detection still correctly uses only closed candles (no lookahead).
    live_price = get_current_price(symbol)
    is_market_leader = symbol in ("BTCUSDT", "ETHUSDT")
    for tf in timeframes:
        df = get_klines(symbol, tf, cfg["candle_limit"])
        if df is None or len(df) < 60:
            if df is None:
                print(f"  [skip] {symbol} {tf}: no data (bad symbol or API error)")
            continue
        df = enrich(df, cfg)
        signals = run_all_detectors(df, cfg)
        if signals:
            close = live_price if live_price is not None else float(df["close"].iloc[-1])
            atr = float(df["atr"].iloc[-1])
            risk_cfg = cfg.get("risk", {})
            signals = attach_atr_risk(signals, close, atr,
                                      risk_cfg.get("atr_multiplier", 1.5),
                                      risk_cfg.get("reward_risk_ratio", 2.0),
                                      risk_cfg.get("max_stop_pct"))
            bias, strength = confluence_score(signals, weights)
            # Regime is descriptive context, not a hard filter - a choppy
            # reading doesn't block an alert, but tells you to size down /
            # be more skeptical of continuation-style signals right now.
            regime = regime_label(df["close"].values, len(df) - 1)
            # BTC/ETH trend agreeing with THIS trade's direction means the
            # setup is more likely just beta (see risk.py's market_disagrees
            # docstring) - BTC/ETH themselves are the market leaders, so the
            # filter doesn't apply to trading them against themselves.
            btc_bull = None if is_market_leader else (btc_trend or {}).get(tf)
            eth_bull = None if is_market_leader else (eth_trend or {}).get(tf)
            if is_market_leader:
                market_disagrees = True
            elif btc_bull is None or eth_bull is None:
                market_disagrees = None
            else:
                trade_is_bullish = bias == "bullish"
                market_disagrees = (btc_bull != trade_is_bullish) and (eth_bull != trade_is_bullish)
            risk = setup_risk_plan(signals, bias, close, risk_cfg.get("min_risk_reward", 1.0),
                                   avg_returns, risk_cfg.get("min_calibrated_move_pct", 0.3),
                                   risk_cfg.get("account_size"), risk_cfg.get("account_risk_pct", 1.0),
                                   unreliable, market_disagrees)
            if risk:
                risk["recent_form"] = detector_recent_form(risk["based_on"], bias,
                                                           cfg.get("journal", {}).get("form_lookback", 5))
            result["timeframes"][tf] = {
                "close": close,
                "bias": bias,
                "strength": strength,
                "regime": regime,
                "signals": signals,
                "risk": risk,
            }
        time.sleep(0.15)  # be polite to the API
    return result


def main():
    cfg = load_config()
    if cfg["scan_all"]:
        pairs = get_all_usdt_pairs()
    elif cfg.get("pairs_mode") == "top_volume":
        # Refetched fresh on every run (not cached/daily) - which coins are
        # liquid enough to scan shifts constantly, so this must be as
        # current as the scan itself, not a snapshot from hours/days ago.
        pairs = get_top_pairs_by_volume(cfg.get("top_n_pairs", 100))
        if not pairs:
            print("[warn] could not fetch top-volume pairs this run - falling back to static pairs list")
            pairs = cfg["pairs"]
    else:
        pairs = cfg["pairs"]
    timeframes = cfg["timeframes"]
    min_conf = cfg["output"]["min_confluence"]
    weights = load_detector_weights(cfg)
    risk_cfg = cfg.get("risk", {})
    # Detectors proven to lose money at the actual stop/target sizing used
    # here - a much harder, more honest bar than backtest_results.json's
    # plain forward-return check (see risk.py's setup_risk_plan docstring).
    # Two sources, pooled by sample size (not a live-overrides-backtest
    # rule - see combined_detector_expectancy's docstring for why that was
    # wrong): the large-scale historical simulation (realistic_backtest.py,
    # thousands of trades) supplies most of the weight until the live
    # journal has accumulated enough real trades of its own to meaningfully
    # shift the verdict. Either way, refused as a trade basis entirely, no
    # matter how tight the stop looks.
    min_n = risk_cfg.get("min_reliable_n", 10)
    min_expectancy = risk_cfg.get("min_detector_expectancy", 0.0)
    expectancy = combined_detector_expectancy(load_realistic_backtest_expectancy(min_n), detector_expectancy(min_n))
    unreliable = {k for k, exp in expectancy.items() if exp < min_expectancy}
    # Target calibration also uses the live journal (real, stop-respecting
    # outcome_pct), not backtest_results.json's naive forward return - same
    # reason as the blacklist above, and same min_n gate so a target can't
    # get calibrated off a handful of early results either.
    avg_returns = detector_avg_return(min_n)
    # BTC/ETH's own current trend per timeframe, fetched once and shared
    # across every pair's scan - see risk.setup_risk_plan's market_disagrees
    # docstring for why an altcoin setup needs to disagree with both to count.
    btc_trend = get_market_trend("BTCUSDT", timeframes, cfg)
    eth_trend = get_market_trend("ETHUSDT", timeframes, cfg)

    print(f"Scanning {len(pairs)} pairs x {len(timeframes)} timeframes...")
    if weights:
        print(f"(strength weighted by backtested edge - {len(weights)} detector/direction weights loaded)\n")
    else:
        print("(no backtest_results.json found - strength is unweighted; run backtest.py to enable weighting)\n")
    if unreliable:
        print(f"(blacklisted as trade basis - negative expectancy at their own R:R: "
              f"{', '.join(f'{n}/{d} ({expectancy[(n,d)]:+.2f}R)' for n, d in sorted(unreliable))})\n")
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "results": []}

    # Pairs are scanned concurrently (each pair's own timeframes are still
    # fetched sequentially inside scan_pair) - this is what makes a 100-pair
    # x 9-timeframe run fit inside a 15-minute schedule instead of taking
    # ~13 minutes serially. get_klines already retries with backoff on 429s,
    # so a burst of concurrent requests self-throttles rather than failing.
    concurrency = cfg.get("scan_concurrency", 8)

    def _scan(symbol):
        try:
            return symbol, scan_pair(symbol, timeframes, cfg, weights, avg_returns, unreliable,
                                     btc_trend, eth_trend)
        except Exception as e:
            print(f"  [error] {symbol}: {e}")
            return symbol, None

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_scan, symbol) for symbol in pairs]
        for future in as_completed(futures):
            symbol, res = future.result()
            if not res or not res["timeframes"]:
                continue
            res = annotate_htf(res, timeframes)
            res["max_strength"] = max(d["strength"] for d in res["timeframes"].values())
            report["results"].append(res)

            # console output for setups meeting min confluence
            for tf, data in res["timeframes"].items():
                if data["strength"] >= min_conf:
                    if cfg.get("mtf", {}).get("require_agreement", False) and not data["htf_agrees"]:
                        continue
                    print(f"{symbol} [{tf}]  {data['bias'].upper()} (strength {data['strength']})  close={data['close']:.6g}  [HTF: {data['htf_note']}]  [regime: {data['regime']}]")
                    for s in data["signals"]:
                        print(f"    - {s['name']}: {s['detail']}")
                    if data["risk"]:
                        r = data["risk"]
                        rr = f"{r['risk_reward']}:1" if r["risk_reward"] else "n/a"
                        print(f"    risk: entry={r['entry']:.6g} stop={r['stop']:.6g} "
                              f"target={r['target']:.6g} (R:R {rr}, based on {r['based_on']}, "
                              f"target: {r['target_basis']})")
                        if r.get("position"):
                            p = r["position"]
                            print(f"    position: risk {p['account_risk_pct']}% (${p['dollar_risk']}) "
                                  f"-> {p['units']:g} units (~${p['position_value']})")
                        if r.get("recent_form"):
                            f = r["recent_form"]
                            print(f"    recent form for {r['based_on']}/{data['bias']}: "
                                  f"{f['wins']}W-{f['losses']}L (last {f['n']})")
                    print()

    report["results"].sort(key=lambda r: r["max_strength"], reverse=True)

    if cfg["output"]["mode"] in ("json", "both"):
        out = Path(__file__).parent / cfg["output"]["json_path"]
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nFull report written to {out}")

    new_keys = None
    if cfg.get("journal", {}).get("enabled", True):
        logged = log_signals(report, cfg)
        if logged:
            print(f"Logged {len(logged)} new setup(s) to journal.jsonl")
        # Only these are genuinely new setups this run - notify_report uses
        # this to skip re-alerting a setup that's already open and unchanged.
        new_keys = {(e["symbol"], e["timeframe"], e["based_on"]) for e in logged}

    sent, sent_keys = notify_report(report, cfg, new_keys=new_keys)
    if sent:
        print(f"Sent {sent} Telegram alert(s)")
        mark_notified(sent_keys)


if __name__ == "__main__":
    main()
