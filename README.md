# Binance Multi-Timeframe Pattern Scanner

Scans Binance pairs across 15m → 1M timeframes for technical setups, scores
confluence weighted by each detector's own backtested edge, attaches a
stop/target risk plan, sends Telegram alerts, and logs every alert to a
forward-tested journal. Runs automatically in the cloud via GitHub Actions —
no server or always-on PC required.

**This is a SIGNAL/ALERT system, not a trading bot.** No orders are placed.
No API keys are needed for market data — Binance public data is free.
Only Telegram credentials (for alerts) are required, and those are never
hardcoded (see "Secrets" below).

---

## What it detects

- **Trend/momentum:** EMA stack & cross, StochRSI extremes, volume spikes
- **Support/resistance:** level clustering, breaks, tests
- **Chart patterns:** double top/bottom, head & shoulders (+ inverse),
  ascending/descending/symmetrical triangles, rising/falling wedges,
  bull/bear flags & pennants
- **Candlesticks:** engulfing, hammer, shooting star
- **Momentum divergence:** RSI divergence at oversold/overbought extremes

Every detector's historical win rate is measured in `backtest.py` and used
to weight its contribution to a setup's `strength` score — a pattern with
real backtested edge (e.g. ascending triangles) counts for more than one
with none (e.g. plain engulfing candles).

## Risk plan

Chart patterns compute stop/target from their own geometry (pattern height
projected past the breakout, invalidation above/below the pattern extreme).
Everything else falls back to an ATR-based stop, capped at `max_stop_pct`
of price so high-timeframe candles (1w/1M) don't produce absurdly wide
stops.

## Live journal

Every setup that clears `min_confluence` gets logged to `journal.jsonl`.
`journal_check.py` later checks real price action to see whether stop or
target was hit first, building an actual forward-tested track record —
not just a retrospective backtest.

---

## Running locally

```bash
pip install -r requirements.txt
python main.py            # one scan, prints + writes signals_output.json
python backtest.py        # measure detector win rates -> backtest_results.json
python journal_check.py   # resolve open journal entries against real price
python smoke_test.py      # offline sanity check, no network needed
```

Edit `config/settings.yaml` to change pairs, timeframes, and every
threshold mentioned above. Set `scan_all: true` to scan every USDT
perpetual instead of just the configured `pairs` list.

## Running in the cloud (GitHub Actions)

Two scheduled workflows in `.github/workflows/`:

- **`scan.yml`** — runs `main.py` + `journal_check.py` every 30 minutes,
  commits `journal.jsonl` back to the repo so history persists across runs.
- **`backtest.yml`** — runs `backtest.py` weekly, commits a refreshed
  `backtest_results.json` so detector weights stay current.

Both can also be triggered manually from the repo's **Actions** tab via
**Run workflow**.

### Secrets

Set these in the repo's **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | your numeric Telegram chat ID |

Locally, put the same two keys in a `.env` file (see `.env.example`) —
`scanner/notify.py` checks environment variables first (for CI), then
falls back to `.env` (for local runs). `.env` is gitignored; never commit it.

---

## Architecture

```
main.py                  orchestrator: scan -> weight -> risk -> notify -> journal
backtest.py               replays history through detectors, measures win rates
journal_check.py          resolves open journal entries against real price
config/settings.yaml      every tunable threshold
scanner/data.py           Binance REST fetching (public, no key)
scanner/indicators.py     EMA, RSI, StochRSI, ATR, volume MA (pure pandas)
scanner/patterns.py       all detectors + pivot/S-R finder
scanner/mtf.py            higher-timeframe agreement filter
scanner/risk.py           stop/target calculation
scanner/journal.py        forward-tested track record (log + resolve)
scanner/notify.py         Telegram formatting/sending
signals_output.json       machine-readable output of the last scan
backtest_results.json     detector win rates, feeds the weighting in main.py
journal.jsonl             append-only log of every real alert + its outcome
```

## Honest notes on accuracy

No technical-pattern system hits 80%+ win rates on liquid markets — if it
did, the edge would already be arbitraged away. The best detectors here
(ascending triangles, bear flags) backtest around 57-66%; some detectors
(engulfing, plain hammer) show no real edge over baseline and are weighted
down accordingly rather than removed, so they still contribute context
without inflating `strength`. Win rate alone is also the wrong number to
chase — a 45% win rate with 2:1 reward:risk (this system's default target)
is profitable; the risk plan matters as much as the direction call.
