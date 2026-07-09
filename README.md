# Binance Multi-Timeframe Pattern Scanner

Scans Binance pairs across 15m → 1M timeframes, detects technical setups
(EMA structure, S/R breaks, StochRSI extremes, volume spikes, triangle
compression), scores confluence, and outputs console + JSON reports.

**This is a SIGNAL/ALERT system, not a trading bot.** No orders are placed.
No API keys are needed for this phase — Binance public market data is free.

---

## Phase 1: Run on your PC

```bash
# 1. Clone (after you push this to GitHub) or copy the folder
cd binance-scanner

# 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python main.py
```

Edit `config/settings.yaml` to change pairs, timeframes, and thresholds.
Set `scan_all: true` to scan every USDT perpetual pair (takes much longer).

---

## Phase 1b: Working on this with Claude Code

1. Push this folder to a GitHub repo:
   ```bash
   git init && git add . && git commit -m "initial scanner"
   git remote add origin https://github.com/<you>/binance-scanner.git
   git push -u origin main
   ```
2. Open Claude Code in the project folder (`claude` in the terminal).
3. Useful first prompts for Claude Code:
   - "Run main.py and fix any errors"
   - "Add a backtest module that replays historical klines through
     patterns.py and logs whether each signal was profitable after N candles"
   - "Add a Telegram notifier that sends any setup with strength >= 3"
   - "Add multi-timeframe confluence: only report a 1h signal if the 4h
     and 1d bias agree"

### Key handling rule (tell Claude Code this)
> Never hardcode keys. When a feature needs a key (Telegram bot token,
> Binance API key, Claude/Gemini API key), stop and ask me for it, then
> read it from a `.env` file that is in `.gitignore`.

Keys you will need LATER (not now):
| Phase | Key | Purpose |
|---|---|---|
| Notifications | Telegram bot token | Push alerts to your phone |
| AI synthesis | Claude or Gemini API key | Turn JSON signals into readable analysis |
| Account data | Binance API key (READ-ONLY) | Balances, positions |
| Execution (much later) | Binance API key (trade-enabled) | Only after months of proven alerts |

---

## Phase 2: Cloud (GCP)

- Containerize with a simple Dockerfile
- Deploy to Cloud Run
- Trigger with Cloud Scheduler at candle closes (e.g. every 4h)
- Store signal history in Firestore → this becomes your hit-rate tracker
- Keys go in Secret Manager, never in the repo

## Phase 3: Feedback loop

- Log every signal + what price did 5/10/20 candles later
- After a few hundred signals you'll know which detectors have edge
  and which are noise — tune settings.yaml from DATA, not feelings

---

## Architecture

```
main.py                 orchestrator
config/settings.yaml    all tunable behavior
scanner/data.py         Binance REST fetching (public, no key)
scanner/indicators.py   EMA, RSI, StochRSI, ATR, volume MA (pure pandas)
scanner/patterns.py     detectors + S/R level finder + confluence
signals_output.json     machine-readable output for AI/notifier layers
```
