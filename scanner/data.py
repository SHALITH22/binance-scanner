"""
Binance market data layer.

IMPORTANT: Public market data (klines/candles) requires NO API key.
You only need API keys later, for account data or placing orders.

Binance.com (both spot and futures) hard-blocks US-region IPs, which is
exactly what GitHub Actions runners use - confirmed via a live run where
every single request came back 451. Binance.US is a separate platform
built for US users and serves the same public kline format, so it's used
as a fallback when binance.com is geo-blocked. Locally (non-US IPs) the
binance.com endpoints work directly and Binance.US is never touched.
"""

import time
import requests
import pandas as pd

BASE_URL = "https://api.binance.com"
FUTURES_URL = "https://fapi.binance.com"
US_URL = "https://api.binance.us"

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]


def _endpoint_chain(futures: bool, kind: str) -> list[str]:
    """Ordered list of URLs to try: requested endpoint, its binance.com
    sibling, then Binance.US - deduped in case futures=False collapses
    the first two."""
    path = f"fapi/v1/{kind}" if futures else f"api/v3/{kind}"
    sibling_path = f"api/v3/{kind}" if futures else f"fapi/v1/{kind}"
    chain = [
        f"{FUTURES_URL if futures else BASE_URL}/{path}",
        f"{BASE_URL if futures else FUTURES_URL}/{sibling_path}",
        f"{US_URL}/api/v3/{kind}",
    ]
    seen = set()
    return [u for u in chain if not (u in seen or seen.add(u))]


def get_all_usdt_pairs(futures: bool = True) -> list[str]:
    """Fetch every actively trading USDT pair, falling back across endpoints on geo-block."""
    resp = None
    for url in _endpoint_chain(futures, "exchangeInfo"):
        resp = requests.get(url, timeout=15)
        if resp.status_code != 451:
            break
        futures = False  # anything past the first hop has no contractType field
    resp.raise_for_status()
    symbols = resp.json()["symbols"]
    return [
        s["symbol"] for s in symbols
        if s["symbol"].endswith("USDT")
        and s.get("status", s.get("contractStatus")) == "TRADING"
        # futures exchangeInfo also lists delivery contracts - keep perpetuals only
        and (not futures or s.get("contractType") == "PERPETUAL")
    ]


# Stable-to-stable pairs (e.g. USDCUSDT) aren't meaningful for pattern
# scanning - near-zero volatility by design, not a "trade".
STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "PYUSD",
    "USDS", "USDD", "GUSD", "FRAX", "LUSD", "SUSD", "USDP", "EURT", "EURC",
}


def get_top_pairs_by_volume(n: int = 100, futures: bool = True) -> list[str]:
    """
    Top N USDT perpetuals by 24h quote volume, fetched fresh on every call -
    which coins are liquid enough to scan genuinely shifts day to day, so
    this is recomputed each run rather than read from a static list.
    One request returns every symbol's volume at once, so this costs a
    single extra API call regardless of N.
    """
    valid = set(get_all_usdt_pairs(futures))
    ticker = None
    for url in _endpoint_chain(futures, "ticker/24hr"):
        resp = requests.get(url, timeout=15)
        if resp.status_code == 451:
            continue
        resp.raise_for_status()
        ticker = resp.json()
        break
    if not ticker:
        return []

    rows = [t for t in ticker if t["symbol"] in valid
            and t["symbol"][:-4] not in STABLE_SYMBOLS]
    rows.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in rows[:n]]


def get_klines(symbol: str, interval: str, limit: int = 300,
               futures: bool = True, max_retries: int = 3) -> pd.DataFrame | None:
    """
    Fetch OHLCV candles for a symbol/interval.
    Tries the requested endpoint first (futures by default); on a 451
    (geo-blocked) response, moves to the next endpoint in the chain
    immediately instead of wasting retries on a domain that will never
    succeed for this IP. See module docstring for the fallback chain.
    Returns a DataFrame with numeric columns, or None on failure.
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for url in _endpoint_chain(futures, "klines"):
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code == 451:  # geo-blocked on this domain - try the next one
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
