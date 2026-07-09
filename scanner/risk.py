"""
Risk levels (stop-loss / target) for each signal.

Structural patterns (double top/bottom, head & shoulders, triangle/wedge,
flag/pennant) already compute geometry-based stop/target inside
patterns.py, since that's where the pivot points and necklines are known.
This module fills in a generic ATR-based stop/target for every other
signal (trend/momentum/S-R/candlestick/divergence), and picks one
consolidated entry/stop/target for the overall setup shown in the console
and Telegram alert.
"""

STRUCTURAL_NAMES = {
    "double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders",
    "ascending_triangle", "descending_triangle", "rising_wedge", "falling_wedge",
    "bull_flag", "bear_flag", "bull_pennant", "bear_pennant",
}


def attach_atr_risk(signals: list[dict], close: float, atr: float, atr_mult: float,
                    reward_risk: float, max_stop_pct: float | None = None) -> list[dict]:
    """
    Fill stop/target for any signal that doesn't already carry structural
    levels. ATR scales with candle size, so on high timeframes (1w/1M) a
    fixed multiplier can produce a stop 30-40% away from price - useless as
    an actual risk plan. max_stop_pct caps the distance to a sane fraction
    of price regardless of how wide the raw ATR is.
    """
    if atr is None or atr <= 0:
        return signals
    distance = atr * atr_mult
    if max_stop_pct is not None:
        distance = min(distance, close * max_stop_pct / 100)
    for s in signals:
        if "stop" in s and "target" in s:
            continue
        if s["direction"] == "bullish":
            s["stop"] = close - distance
            s["target"] = close + distance * reward_risk
        elif s["direction"] == "bearish":
            s["stop"] = close + distance
            s["target"] = close - distance * reward_risk
    return signals


def setup_risk_plan(signals: list[dict], bias: str, close: float) -> dict | None:
    """
    Pick one consolidated entry/stop/target for the setup: prefer a
    structural (pattern-based) level over a generic ATR one, since it's
    tied to actual chart geometry rather than a fixed multiple. Among
    structural signals, prefer the one with the tightest (most disciplined)
    stop distance.
    """
    candidates = [s for s in signals
                  if s["direction"] == bias and "stop" in s and "target" in s]
    if not candidates:
        return None
    structural = [s for s in candidates if s["name"] in STRUCTURAL_NAMES]
    pool = structural or candidates
    pick = min(pool, key=lambda s: abs(close - s["stop"]))

    stop, target = pick["stop"], pick["target"]
    risk = abs(close - stop)
    reward = abs(target - close)
    return {
        "entry": round(close, 6),
        "stop": round(stop, 6),
        "target": round(target, 6),
        "risk_reward": round(reward / risk, 2) if risk > 0 else None,
        "based_on": pick["name"],
    }
