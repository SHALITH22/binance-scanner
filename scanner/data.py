"""
Binance market data layer.

IMPORTANT: Public market data (klines/candles) requires NO API key.
You only need API keys later, for account data or placing orders.
"""

import time
import requests
import pandas as pd

BASE_URL = "https://api.binance.com"
FUTURES_URL = "https://fapi.binance.com"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def get_all_usdt_pairs(futures: bool = True) -> list[str]:
    """Fetch every actively trading USDT pair."""
    url = f"{FUTURES_URL}/fapi/v1/exchangeInfo" if futures else f"{BASE_URL}/api/v3/exchangeInfo"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    symbols = resp.json()["symbols"]
    return [
        s["symbol"] for s in symbols
        if s["symbol"].endswith("USDT")
        and s.get("status", s.get("contractStatus")) == "TRADING"
        # futures exchangeInfo also lists delivery contracts - keep perpetuals only
        and (not futures or s.get("contractType") == "PERPETUAL")
    ]


def get_klines(symbol: str, interval: str, limit: int = 300,
               futures: bool = True, max_retries: int = 3) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles for a symbol/interval.
    Returns a DataFrame with numeric columns, or None on failure.
    """
    url = f"{FUTURES_URL}/fapi/v1/klines" if futures else f"{BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:  # rate limited - back off
                wait = int(resp.headers.get("Retry-After", 5))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None

            df = pd.DataFrame(data, columns=KLINE_COLUMNS)
            for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
                df[col] = pd.to_numeric(df[col])
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
            # Drop the still-forming candle: only closed candles are valid for signals
            return df.iloc[:-1].reset_index(drop=True)

        except requests.RequestException as e:
            if attempt == max_retries - 1:
                print(f"  [data] {symbol} {interval} failed: {e}")
                return None
            time.sleep(2 ** attempt)
    return None
