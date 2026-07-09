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
    """
    Fetch every actively trading USDT pair. Falls back to the spot endpoint
    if the futures one is geo-blocked (451) - see get_klines for why.
    """
    url = f"{FUTURES_URL}/fapi/v1/exchangeInfo" if futures else f"{BASE_URL}/api/v3/exchangeInfo"
    resp = requests.get(url, timeout=15)
    if resp.status_code == 451 and futures:
        url = f"{BASE_URL}/api/v3/exchangeInfo"
        resp = requests.get(url, timeout=15)
        futures = False  # spot has no contractType field, skip the PERPETUAL filter below
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
    Tries the requested endpoint first (futures by default); if Binance
    returns 451 (geo-blocked - happens for cloud CI IPs like GitHub Actions
    runners, which Binance treats as a restricted region), falls back to
    the other endpoint (spot<->futures) instead of wasting retries on a
    domain that will never succeed for this IP.
    Returns a DataFrame with numeric columns, or None on failure.
    """
    primary = f"{FUTURES_URL}/fapi/v1/klines" if futures else f"{BASE_URL}/api/v3/klines"
    fallback = f"{BASE_URL}/api/v3/klines" if futures else f"{FUTURES_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for url in (primary, fallback):
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code == 451:  # geo-blocked on this domain - try the other one
                    break
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
                    print(f"  [data] {symbol} {interval} failed on {url}: {e}")
                    break
                time.sleep(2 ** attempt)
    return None
