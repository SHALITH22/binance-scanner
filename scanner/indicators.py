"""
Indicator layer - pure pandas implementations (no TA-Lib compilation headaches).
Every function takes a klines DataFrame and returns it with new columns.
"""

import pandas as pd


def add_emas(df: pd.DataFrame, periods: list[int]) -> pd.DataFrame:
    for p in periods:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def add_rsi(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))
    return df


def add_stochrsi(df: pd.DataFrame, rsi_length: int = 14,
                 stoch_length: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    if "rsi" not in df.columns:
        df = add_rsi(df, rsi_length)
    rsi = df["rsi"]
    lowest = rsi.rolling(stoch_length).min()
    highest = rsi.rolling(stoch_length).max()
    stoch = 100 * (rsi - lowest) / (highest - lowest).replace(0, 1e-10)
    df["stochrsi_k"] = stoch.rolling(k).mean()
    df["stochrsi_d"] = df["stochrsi_k"].rolling(d).mean()
    return df


def add_volume_ma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    df["volume_ma"] = df["volume"].rolling(period).mean()
    return df


def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Average True Range - used for stop distance / volatility context."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / length, adjust=False).mean()
    return df


def enrich(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Apply all configured indicators in one pass."""
    ind = cfg["indicators"]
    df = add_emas(df, ind["ema_periods"])
    s = ind["stochrsi"]
    df = add_stochrsi(df, s["rsi_length"], s["stoch_length"], s["k"], s["d"])
    df = add_volume_ma(df, ind["volume_ma_period"])
    df = add_atr(df)
    return df
