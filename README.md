# Brain — a personal autonomous stock-research engine

Robinhood opened their platform to AI agents (May 2026) but ships **no
intelligence** — they're the rails, you bring the brain. This is the brain.

It reads your **real** portfolio (read-only), researches the market, learns your
risk personality, and hands you fully-reasoned trade tickets. **You execute the
trades yourself** — the brain never places an order. That's deliberate: the
hard, valuable part is the research; execution is the swappable last 5%.

## What it does

- **Discovery engine** — a quantitative screen finds candidates outside your
  holdings; the LLM writes the thesis and ranks them for *you*. Ask for stable
  or volatile ideas.
- **Portfolio guardian** — watches every holding (signals + news), flags
  good/bad developments and concentration risk, gives you a daily digest.
- **Analyst** — on-demand deep dive on any ticker → a falsifiable trade ticket.
- **Risk/personality engine** — every recommendation is filtered through your
  profile, which *learns* from what you accept and reject.
- **Shadow mode** — every recommendation is logged as a paper trade so you get
  an honest track record *before* trusting it with money.

## Architecture

```
web dashboard / CLI
        │
   orchestrator ──────────────┐
        │                     │
  engines: discovery · guardian · analyst
        │                     │
   data layer (yfinance, RSS news) · risk profile · shadow ledger
        │
   portfolio source (manual  |  read-only Robinhood)   ← swappable
```

The LLM (Claude Opus 4.7, adaptive thinking, prompt-cached system prompt) only
*synthesizes and reasons* over grounded data — it never predicts prices or
picks tickers ungrounded.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY
```

Choose your portfolio source in `.env`:
- `PORTFOLIO_SOURCE=manual` (default) — edit holdings in the dashboard. No creds.
- `PORTFOLIO_SOURCE=robinhood` — read-only login via `robin_stocks` (reads
  positions, never trades). Set `RH_USERNAME`/`RH_PASSWORD` (and `RH_MFA` if you
  want automated TOTP — `pip install pyotp`). Against RH ToS; read-only is low risk.

## Run

```bash
uvicorn web.app:app --reload      # dashboard at http://127.0.0.1:8000
```

Or the CLI:

```bash
python cli.py analyze NVDA
python cli.py discover --flavor stable
python cli.py digest
python cli.py ask "what's the biggest risk in my portfolio?"
python cli.py score
```

## Scheduling the daily digest

The guardian is built to run on a schedule. Simplest: a cron entry calling
`python cli.py digest` each morning (it writes to `data_store/digests/`). Or
wire it to Claude Code's `/schedule` for a remote agent.

## Honest limitations

- Free data (yfinance/RSS) is good enough to prove the brain, not to bet the
  farm. Paid sources slot in behind `brain/data/`.
- The discovery universe is a curated starter list (`brain/data/universe.py`) —
  expand it to a full exchange listing for real breadth.
- **Shadow mode will probably humble the brain at first. That's the point.**
  Let the track record earn trust before you act on real money.
- The unofficial Robinhood read path can break on app updates and stores a
  session token locally. Prefer `manual` until you trust the rest.
