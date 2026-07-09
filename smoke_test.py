"""
Offline smoke test: runs the full pipeline (indicators + detectors + confluence)
on synthetic and/or fixture klines - no network needed.
Usage: python smoke_test.py [fixture.json ...]
"""

import json
import sys

import numpy as np
import pandas as pd
import yaml

from scanner.indicators import enrich
from scanner.patterns import run_all_detectors, find_sr_levels, find_pivots
from main import confluence_score, load_config

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def synthetic_df(n=300, seed=42, trend=0.001):
    rng = np.random.default_rng(seed)
    ret = rng.normal(trend, 0.01, n)
    close = 100 * np.exp(np.cumsum(ret))
    o = np.roll(close, 1); o[0] = close[0]
    spread = np.abs(rng.normal(0, 0.005, n)) * close
    df = pd.DataFrame({
        "open": o, "close": close,
        "high": np.maximum(o, close) + spread,
        "low": np.minimum(o, close) - spread,
        "volume": rng.uniform(100, 1000, n),
    })
    # inject a volume spike on last candle
    df.loc[n - 1, "volume"] = df["volume"].mean() * 5
    return df


def fixture_df(path):
    raw = json.load(open(path))
    df = pd.DataFrame(raw, columns=KLINE_COLUMNS)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    return df.iloc[:-1].reset_index(drop=True)  # drop forming candle


def run_case(name, df, cfg):
    df = enrich(df, cfg)
    assert not df[["ema_20", "ema_50", "ema_200", "stochrsi_k", "atr"]].iloc[-1].isna().any(), \
        f"{name}: NaN indicators on last candle"
    pivot_highs, pivot_lows = find_pivots(df, cfg["signals"].get("pattern_lookback", 150))
    levels = find_sr_levels(pivot_highs, pivot_lows, cfg["signals"]["sr_tolerance_pct"], cfg["signals"]["sr_min_touches"])
    signals = run_all_detectors(df, cfg)
    bias, strength = confluence_score(signals) if signals else ("none", 0)
    print(f"{name}: {len(levels)} S/R levels | {len(signals)} signals | bias={bias} strength={strength}")
    for s in signals:
        print(f"    - {s['name']} [{s['direction']}]: {s['detail']}")


if __name__ == "__main__":
    cfg = load_config()
    run_case("synthetic-uptrend", synthetic_df(trend=0.002), cfg)
    run_case("synthetic-downtrend", synthetic_df(seed=7, trend=-0.002), cfg)
    run_case("synthetic-flat", synthetic_df(seed=3, trend=0.0), cfg)
    for path in sys.argv[1:]:
        run_case(f"fixture:{path}", fixture_df(path), cfg)
    print("\nSmoke test PASSED")
