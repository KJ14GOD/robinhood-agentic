# uAutopilot

Autopilot is the paper-fund twin of the real portfolio. It clones the account once,
then manages that clone with fixed capital so its decisions can be measured against
the user's real book without touching real money.

## Core Contract

- Autopilot never places real Robinhood orders.
- At launch, it copies the real account's cash, positions, and starting value.
- After launch, it is a separate paper account. It does not re-sync positions from
the real account.
- It cannot invent cash. Buys can only use paper cash already in the Twin or cash
raised by paper sells/trims.
- It cannot sell more shares than it owns.
- Off-hours decisions are queued. Fills only happen during regular market hours.
- If stale pending batches exist, the newest pending batch supersedes older ones.
- History must stay auditable: every proposed, canceled, and filled paper trade
remains visible.

## Universe Policy

Autopilot should not be limited to current holdings or a tiny watchlist. It can
consider a broad tradable universe, but every candidate must be grounded before the
LLM can choose it.

Candidate sources:

- Current Twin holdings.
- User watchlist and stored theses.
- Active strategy mission candidates.
- Broad market screen over the bundled S&P 500 plus curated high-interest stocks,
ADRs, ETFs, infrastructure names, defense/space names, energy/nuclear names,
and other liquid story stocks.
- Optional local additions from `data_store/universe_extra.json`.
- Recent persisted events/catalysts that mention a ticker.

The LLM may not make up tickers. If a name is not in the grounded candidate list,
it should not be traded in that cycle.

## Decision Shape

Every move should declare:

- Action: `buy`, `add`, `trim`, `sell`, or `hold`.
- Dollar size.
- Tactic: why this trade exists as a strategy.
- Horizon: how long the idea should be judged over.
- Thesis: why Autopilot wants exposure after the move.
- Exit rule: what would make the position wrong.
- Review window: when the self-review loop should judge the move.

## Pre-Trade Critic

The LLM is the portfolio manager, not the executor. Before anything gets queued,
a deterministic critic/governor reviews the proposed batch.

The critic can:

- Reject ungrounded tickers that are not in the candidate universe.
- Reject sells/trims for names Autopilot does not own.
- Cap sell/trim dollars to the actual paper value owned.
- Cap buy/add dollars to the configured single-position limit.
- Scale buy/add dollars down to available Twin cash plus same-cycle sells/trims.
- Size down or reject tactics that have poor reviewed results.
- Write a `critic_note` so the History view explains every adjustment.

This keeps Autopilot capital-aware. The model can propose a trade, but the
executor decides whether it is legal and properly sized.

Tactics should be explicit, for example:

- `rebalance`
- `risk_reduction`
- `momentum_continuation`
- `pullback_in_uptrend`
- `valuation_mean_reversion`
- `catalyst_trade`
- `long_term_compounder`
- `theme_exposure`
- `defensive_rotation`
- `liquidity_cleanup`

## Stage 3: Self-Review Learning Loop

Stage 3 is not reinforcement learning yet. It is policy learning from measured
paper trades.

Loop:

1. Queue or fill a paper trade with tactic, horizon, thesis, and review window.
2. At fill time, capture actual fill price and benchmark anchor.
3. After the review window matures, mark the trade to current price.
4. Score it versus the market benchmark.
5. Store a review note and outcome.
6. Feed tactic-level lessons into future Autopilot prompts.

The system should learn things like:

- Pullback entries above the 200-day average are working or failing.
- Concentration trims reduce risk but may trim winners too early.
- Catalyst trades are paying off or becoming noise.
- High-conviction calls are or are not outperforming low-conviction calls.

Early implementation should be deterministic and auditable. Later, once enough
trade history exists, this can become a contextual bandit or RL-style policy
layer. The learned policy should influence ranking and sizing, never bypass
capital/risk guardrails.

## Stage 4: Shorting

Shorting is parked until Stage 3 proves discipline.

Before shorting, the system needs:

- Margin model.
- Borrow/financing assumptions.
- Short squeeze risk limits.
- Max loss controls.
- Stop/cover rules.
- Portfolio beta and hedge accounting.

The first step should be hedging behavior, not naked shorts.