"""
Telegram notifier.

Keys are NEVER hardcoded. Two ways to supply them, checked in this order:
  1. Process environment variables (e.g. GitHub Actions secrets passed via
     the workflow's `env:` block) - preferred for cloud/scheduled runs,
     never touches disk.
  2. A local `.env` file (gitignored) - for running on your own machine:
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=123456789

Get a token from @BotFather; get your chat id from @userinfobot.
If neither source has the values, the notifier silently no-ops (scanner still runs).
"""

import os
from html import escape
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def load_env(path: Path = ENV_PATH) -> dict:
    env = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())  # env vars take priority over .env
    return env


def send_telegram(text: str, env: dict | None = None) -> bool:
    env = env if env is not None else load_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=15)
        return r.ok
    except requests.RequestException:
        return False


def format_setup(symbol: str, tf: str, data: dict, generated_at: str | None = None) -> str:
    lines = [f"<b>{escape(symbol)}</b> [{escape(tf)}] {escape(data['bias'].upper())} "
             f"(strength {data['strength']})  close={data['close']:.6g}"]
    if generated_at:
        # Price moves between this scan and whenever you actually read the
        # message - this timestamp lets you judge how stale it might be by
        # the time you act, rather than assuming the quoted price is "now".
        lines.append(f"Scanned: {escape(generated_at)} UTC")
    if "htf_note" in data:
        lines.append(f"HTF: {escape(data['htf_note'])}")
    if data.get("regime"):
        lines.append(f"Regime: {escape(data['regime'])}"
                     + (" - lower conviction, market is choppy right now" if data["regime"] == "choppy" else ""))
    for s in data["signals"]:
        lines.append(f"- {escape(s['name'])}: {escape(s['detail'])}")
    if data.get("risk"):
        r = data["risk"]
        rr = f"{r['risk_reward']}:1" if r["risk_reward"] else "n/a"
        lines.append(f"Risk: entry {r['entry']:.6g} / stop {r['stop']:.6g} / "
                     f"target {r['target']:.6g} (R:R {rr}, based on {escape(r['based_on'])}, "
                     f"target: {escape(r['target_basis'])})")
        if r.get("position"):
            p = r["position"]
            lines.append(f"Position size: risk {p['account_risk_pct']}% (${p['dollar_risk']}) "
                         f"-&gt; {p['units']:g} units (~${p['position_value']})")
        if r.get("recent_form"):
            f = r["recent_form"]
            lines.append(f"Recent form for {escape(r['based_on'])}/{escape(data['bias'])}: "
                         f"{f['wins']}W-{f['losses']}L (last {f['n']})")
    return "\n".join(lines)


def notify_report(report: dict, cfg: dict) -> int:
    """Send one message per qualifying setup. Returns number sent."""
    tg = cfg.get("notify", {}).get("telegram", {})
    if not tg.get("enabled", False):
        return 0
    env = load_env()
    if not env.get("TELEGRAM_BOT_TOKEN"):
        print("  [notify] telegram enabled but TELEGRAM_BOT_TOKEN not found in "
              "environment or .env - skipping")
        return 0
    min_strength = tg.get("min_strength", 3)
    only_agreeing = tg.get("only_htf_agreeing", True)
    # "2026-07-10T05:23:11.123456+00:00" -> "05:23:11" - just the clock time,
    # readable at a glance in a chat message.
    generated_at = report.get("generated_at", "")
    scan_time = generated_at[11:19] if len(generated_at) >= 19 else None
    sent = 0
    for res in report["results"]:
        for tf, data in res["timeframes"].items():
            if data["strength"] < min_strength:
                continue
            if only_agreeing and not data.get("htf_agrees", True):
                continue
            if send_telegram(format_setup(res["symbol"], tf, data, scan_time), env):
                sent += 1
    return sent
