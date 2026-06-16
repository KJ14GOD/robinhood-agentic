# Autopilot

Autopilot is Signal's autonomous paper-fund twin. It clones the real portfolio once,
then manages that fixed-capital clone so its decisions can be measured against the
user's real account without touching real money.

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
- History stays auditable: proposed, canceled, resized, and filled paper trades remain visible.

## Autonomous Inputs

Autopilot should not be limited to current holdings or a tiny watchlist. It can
consider a broad tradable universe, but every candidate must be grounded before the
LLM can choose it.

Candidate sources:

- Current Twin holdings.
- User watchlist and stored theses.
- Active user-created strategy mission candidates.
- Recent persisted events/catalysts that mention a ticker.
- Broad market screen over the bundled S&P 500 plus curated high-interest stocks,
  ADRs, ETFs, infrastructure names, defense/space names, energy/nuclear names,
  and other liquid story stocks.
- Optional local additions from `data_store/universe_extra.json`.
- Autonomous Theme Scout candidates.
- Autonomous Strategy Discovery experiments.

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
- Optional source attribution: autonomous theme and/or autonomous strategy experiment.

## Ordered Execution Plans

Autopilot thinks in ordered batches, not independent parallel orders.

Example:

1. Trim an overweight name.
2. Sell a broken/stub position.
3. Buy a new candidate using the cash raised above.

Each buy can carry `depends_on` metadata so History explains which trims/sells funded it.

## Pre-Trade Critic

The LLM is the portfolio manager, not the executor. Before anything gets queued,
a deterministic critic/governor reviews the proposed batch.

The critic can:

- Reject ungrounded tickers that are not in the candidate universe.
- Reject sells/trims for names Autopilot does not own.
- Cap sell/trim dollars to the actual paper value owned.
- Scale buy/add dollars down to available Twin cash plus same-cycle sells/trims.
- Align a trade to the tactic from an autonomous strategy experiment.
- Size down or reject tactics/contexts that have poor reviewed results.
- Nudge size up modestly when a context has repeatedly worked.
- Write a `critic_note` so the History view explains every adjustment.

There is intentionally no hard single-position comfort cap here. Autopilot is allowed
to decide its own concentration. The hard constraint is fixed capital.

## Pre-Fill Validation

Queued orders are rechecked right before fill.

The preflight layer can:

- Cancel a buy/add if the price moved too far above its decision quote.
- Cancel a buy/add if a fresh thesis-break event appeared after the plan was queued.
- Cancel a non-urgent sell/trim if the name gapped down too far before fill.
- Resize buys if funding legs changed.

These outcomes are shown in History as preflight badges and notes.

## Stage 3: Self-Review Learning Loop

Stage 3 is policy learning from measured paper trades.

Loop:

1. Queue or fill a paper trade with tactic, horizon, thesis, exit rule, and review windows.
2. At fill time, capture actual fill price, SPY anchor, sector ETF anchor, market regime,
   and autonomous theme/strategy attribution.
3. After each review window matures, mark the trade to current price.
4. Score it versus SPY and its sector ETF.
5. Check whether the original thesis is `stronger`, `active`, `weakening`, or `broken`.
6. Store a review note and outcome.
7. Feed lessons into future Autopilot prompts and critic sizing.

Long-term ideas get grace windows. A normal early drawdown is not called a failure
unless the thesis actually weakens or breaks.

## Contextual Bandit Policy

Autopilot now has a conservative contextual bandit layer. It does not simulate fantasy
episodes. It uses only judged paper-trade review windows.

It learns by context:

- tactic
- sector ETF
- autonomous theme
- autonomous strategy
- market regime
- tactic + sector
- tactic + regime
- theme + regime
- strategy + regime

The bandit can influence ranking and sizing, but it cannot bypass fixed-capital rules.

Full RL is parked until there is enough real paper-trade history and a backtesting/simulation
layer. Jumping straight to RL without enough episodes would overfit and make worse decisions.

## Autonomous Strategy Discovery

Theme Scout answers: "what areas of the market are alive?"

Strategy Discovery answers: "what tactic should Autopilot test there?"

Each autonomous strategy experiment stores:

- hypothesis
- tactic
- theme
- market regime
- entry rule
- exit/invalidation rule
- sizing note
- candidate roster
- evidence
- score/confidence
- status: `exploring`, `active`, `cooling`, or `retired`

Autopilot can use these experiments as grounded candidates. If it trades one, the source
strategy id flows into the review windows so the experiment can be measured later.

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
