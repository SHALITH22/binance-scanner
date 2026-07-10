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

from math import floor, log10


def round_sig(x: float, sig: int = 6) -> float:
    """
    Round to N significant figures, not N decimal places. round(x, 6) is
    fine for a $63,000 BTC price but destroys almost all precision on a
    $0.00000428 SHIB-style price - entry/stop/target all collapsing to the
    same displayed "4e-06" is exactly that bug, not a coincidence.
    """
    if x == 0 or x != x:  # zero or NaN
        return 0.0
    digits = sig - int(floor(log10(abs(x)))) - 1
    return round(x, digits)


STRUCTURAL_NAMES = {
    "double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders",
    "ascending_triangle", "descending_triangle", "rising_wedge", "falling_wedge",
    "bull_flag", "bear_flag", "bull_pennant", "bear_pennant",
}


def _structural_levels_valid(s: dict, close: float) -> bool:
    """
    Structural patterns (triangle/wedge/H&S/flag) fit a trendline through a
    handful of pivot points and extrapolate it to "now" - when that linear
    fit doesn't track price well, the extrapolated level can land on the
    WRONG side of the current close (e.g. a "resistance" that's actually
    below price), which silently produces a backwards stop/target: a
    bearish trade with its stop BELOW entry instead of above. A stop/target
    pair only makes sense if the stop and target actually bracket price in
    the direction implied - bullish needs stop < close < target, bearish
    needs stop > close > target.
    """
    if s["direction"] == "bullish":
        return s["stop"] < close < s["target"]
    if s["direction"] == "bearish":
        return s["stop"] > close > s["target"]
    return True


def attach_atr_risk(signals: list[dict], close: float, atr: float, atr_mult: float,
                    reward_risk: float, max_stop_pct: float | None = None) -> list[dict]:
    """
    Fill stop/target for any signal that doesn't already carry VALID
    structural levels (see _structural_levels_valid - a structural pattern
    whose geometry produced a backwards stop gets its levels discarded and
    falls through to the generic ATR-based ones below instead of silently
    handing out a broken trade plan). ATR scales with candle size, so on
    high timeframes (1w/1M) a fixed multiplier can produce a stop 30-40%
    away from price - useless as an actual risk plan. max_stop_pct caps the
    distance to a sane fraction of price regardless of how wide the raw
    ATR is.
    """
    if atr is None or atr <= 0:
        return signals
    distance = atr * atr_mult
    if max_stop_pct is not None:
        distance = min(distance, close * max_stop_pct / 100)
    for s in signals:
        if "stop" in s and "target" in s:
            if _structural_levels_valid(s, close):
                continue
            del s["stop"]
            del s["target"]
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


def position_size(entry: float, stop: float, account_size: float | None,
                  account_risk_pct: float) -> dict | None:
    """
    Professional risk management rule: risk a fixed small % of account
    equity per trade, sized off the actual stop distance - never a fixed
    unit count and never scaled by "how sure you feel" about the setup.
    Returns None if account_size isn't configured (we don't guess it).
    """
    if account_size is None or account_size <= 0:
        return None
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return None
    dollar_risk = account_size * account_risk_pct / 100
    units = dollar_risk / risk_per_unit
    return {
        "account_risk_pct": account_risk_pct,
        "dollar_risk": round(float(dollar_risk), 2),
        "units": round_sig(float(units), 6),
        "position_value": round(float(units * entry), 2),
    }


def setup_risk_plan(signals: list[dict], bias: str, close: float,
                    min_risk_reward: float = 1.0, avg_returns: dict | None = None,
                    min_calibrated_move_pct: float = 0.3,
                    account_size: float | None = None,
                    account_risk_pct: float = 1.0,
                    unreliable: set | None = None) -> dict | None:
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

    `unreliable` is a set of (detector_name, direction) pairs to refuse
    outright, regardless of stop tightness or backtested edge - these are
    detectors the live journal has already shown to lose money at this
    stop/target sizing (see journal.detector_reliability). Without this,
    plan selection picks purely on which candidate's stop happens to be
    numerically tightest, which has nothing to do with whether that
    detector has ever actually worked.
    """
    unreliable = unreliable or set()
    candidates = [s for s in signals
                  if s["direction"] == bias and "stop" in s and "target" in s
                  and (s["name"], s["direction"]) not in unreliable]
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
        "entry": round_sig(float(close), 6),
        "stop": round_sig(float(stop), 6),
        "target": round_sig(float(target), 6),
        "risk_reward": round(float(reward / risk), 2) if risk > 0 else None,
        "based_on": pick["name"],
        "target_basis": "historical avg move" if calibrated else "pattern/ATR estimate",
        "position": position_size(close, stop, account_size, account_risk_pct),
    }
