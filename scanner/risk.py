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


def calibrate_target(entry: float, detector_name: str, direction: str,
                     avg_returns: dict, min_move_pct: float = 0.3) -> float | None:
    """
    Override a geometric/ATR target with the detector's actual average
    historical forward return (from backtest_results.json) when available -
    grounds the target in what price has genuinely tended to do after this
    specific setup, instead of a fixed multiple or a "measured move"
    assumption that was never checked against real data.

    avg_returns[(name, direction)] is a raw mean forward return (can be
    negative for a bearish detector that historically worked, since price
    fell) - entry * (1 + pct/100) is correct as-is for either direction,
    no sign-flipping needed. Skipped if the move is too small to be a
    meaningful target (near-zero average moves aren't a "plan").
    """
    avg_pct = avg_returns.get((detector_name, direction))
    if avg_pct is None or abs(avg_pct) < min_move_pct:
        return None
    return entry * (1 + avg_pct / 100)


def setup_risk_plan(signals: list[dict], bias: str, close: float,
                    min_risk_reward: float = 1.0, avg_returns: dict | None = None,
                    min_calibrated_move_pct: float = 0.3) -> dict | None:
    """
    Pick one consolidated entry/stop/target for the setup: prefer a
    structural (pattern-based) level over a generic ATR one, since it's
    tied to actual chart geometry rather than a fixed multiple. The target
    is then replaced with the detector's real historical average move when
    that data is available (see calibrate_target) - the reported scenario
    reflects what has actually happened, not just what the geometry implies.

    Structural stop/target come from independent pieces of geometry though
    (e.g. a flag's stop is the whole consolidation range, its target is
    just the pole height) - nothing guarantees those two distances produce
    a favorable ratio. A candidate is only used if its reward:risk (using
    the calibrated target where available) clears min_risk_reward; if
    nothing does, this returns None rather than presenting a plan that
    risks more than it can gain. Among qualifying candidates, prefer the
    tightest (most disciplined) stop distance.
    """
    candidates = [s for s in signals
                  if s["direction"] == bias and "stop" in s and "target" in s]
    if not candidates:
        return None
    avg_returns = avg_returns or {}

    def effective_target(s):
        calibrated = calibrate_target(close, s["name"], s["direction"], avg_returns, min_calibrated_move_pct)
        return calibrated if calibrated is not None else s["target"]

    def rr(s):
        risk = abs(close - s["stop"])
        return abs(effective_target(s) - close) / risk if risk > 0 else 0

    qualifying = [s for s in candidates if rr(s) >= min_risk_reward]
    if not qualifying:
        return None  # every candidate here risks more than it could gain - not a plan worth acting on

    structural = [s for s in qualifying if s["name"] in STRUCTURAL_NAMES]
    pool = structural or qualifying
    pick = min(pool, key=lambda s: abs(close - s["stop"]))

    stop = pick["stop"]
    target = effective_target(pick)
    calibrated = (target != pick["target"])
    risk = abs(close - stop)
    reward = abs(target - close)
    return {
        "entry": round(close, 6),
        "stop": round(stop, 6),
        "target": round(target, 6),
        "risk_reward": round(reward / risk, 2) if risk > 0 else None,
        "based_on": pick["name"],
        "target_basis": "historical avg move" if calibrated else "pattern/ATR estimate",
    }
