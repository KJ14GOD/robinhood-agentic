# Product Vision

This project is not meant to be a trading bot or a prettier portfolio viewer.
The goal is a personal autonomous portfolio research assistant: a system that
understands the user's holdings, style, strategy, watchlist, and risk limits,
then proactively surfaces what matters.

## North Star

Build a source-aware investing operating system that can:

- track the real Robinhood portfolio,
- remember theses and watchlist ideas,
- learn the user's investing style,
- monitor holdings, themes, sectors, news, and events,
- warn when something important changes,
- find new opportunities without being asked,
- explain recommendations with evidence,
- log what it recommended and whether it was right,
- measure whether its recommendations have real edge,
- keep trade execution manual unless a separate approved execution account is added later.

The product should feel fast, calm, and serious. It should not feel like a
chatbot that wakes up only when clicked.

## Product Shape

Preferred surfaces:

- `Today`: command center for what changed, what matters, and what to ignore.
- `Portfolio`: holdings, allocation, charts, concentration, and account truth.
- `Research` / `Memory`: stored theses, watchlist, strategy missions, ticker dossiers.
- `Assistant`: ask questions and run deeper research.
- `Settings`: investor profile, limits, sources, and automation controls.

Avoid adding many overlapping tabs. New features should attach to one of these
surfaces and read/write durable state.

## Architecture Direction

The long-term system should look like:

```text
Robinhood / prices / news / earnings / filings
        |
        v
Cheap deterministic monitors
price moves, concentration, earnings, watchlist thresholds, thesis keywords
        |
        v
Database
snapshots, positions, theses, events, research, missions, briefings, agent runs
        |
        v
LLM only when useful
explain, rank, summarize, compare, write recommendation cards
        |
        v
UI
Today feed, Portfolio, Memory, Assistant, alerts, briefings
```

Cheap checks should run often. LLM calls should be gated by importance, stale
research, explicit user action, scheduled briefings, or strategy missions.

## Product Identity

There are two possible identities:

1. Research copilot: safest default. The system researches, recommends,
   explains, tracks, and the user manually executes.
2. Execution-with-approval trader: future state only if the system proves
   measurable edge. The assistant proposes, the user approves, and execution
   happens with strict guardrails.

The current product should stay a research copilot until the evaluation layer
can answer: "Is the brain actually good?"

Execution is not a UI feature. It is an identity shift that requires measured
trust first.

## Current Foundation

The app currently has:

- Robinhood portfolio read path.
- Portfolio snapshots in DB.
- Research memory in DB with JSON fallback.
- Watchlist items and theses.
- Briefings.
- Research events foundation.
- Ticker research cache.
- Instant Analyze from cached DB research.
- Deep refresh / refresh thesis to force fresh LLM research.
- Chart handling with live-ish cache behavior.
- Shadow tracking foundation.

This proves the brain can reason over grounded state. It does not yet prove the
brain has investable edge.

## Trust And Evaluation

The most important missing product layer is measured edge.

The app should not only remember recommendations; it should grade them:

- Did high-conviction ideas outperform low-conviction ideas?
- Which engines were right: analyst, discovery, briefing, mission, feed?
- Which signals mattered: momentum, valuation, news, concentration, thesis change?
- Did action labels perform as expected?
- Was the recommendation early, late, or wrong?
- Did the user act, and did manual execution help or hurt?
- How did ideas perform versus SPY/QQQ/sector benchmarks?

The Shadow tab should evolve from a paper-trade log into a true scorecard:

- win rate,
- average return,
- benchmark-relative return,
- calibration by conviction bucket,
- performance by action label,
- performance by risk mode,
- best/worst recommendation families,
- attribution by signal/source,
- stale thesis failures,
- "what the brain is good/bad at" summary.

This is the bottleneck between "interesting assistant" and "something the user
would trust with real money."

## Known Chart Truth

Current portfolio value is Robinhood-backed.

The portfolio chart line is not true historical Robinhood account equity yet.
Robinhood portfolio historicals currently fails through `robin_stocks`, so the
chart falls back to:

```text
current holdings x historical prices, anchored to current broker equity
```

That means the latest value is aligned to Robinhood, but the historical line is
an estimate. True historical equity can only be accurate from stored snapshots
going forward or from a working Robinhood/private portfolio historical source.

## Memory Goal

Memory should eventually understand:

- accepted/rejected ideas,
- favored and avoided sectors,
- stable vs volatile preference,
- preferred position sizing,
- themes the user cares about,
- concentration comfort,
- thesis invalidation patterns,
- strategy missions like defense, AI infrastructure, data-center power, etc.

The assistant should use this memory to decide what is worth surfacing.

Memory should be living, not archival. A stored thesis should be revisited when:

- its invalidation condition appears,
- earnings/guidance contradicts the thesis,
- price action breaks an expected range,
- concentration changes the portfolio risk,
- news strengthens or weakens a named driver,
- the research is stale.

Example target behavior:

```text
You said the AMD thesis breaks if data-center growth disappoints.
New guidance weakened that exact segment.
Decision: EXIT REVIEW.
```

## Strategy Missions

The user should be able to say:

```text
Track defense stocks.
Find stable AI exposure.
Watch volatile data-center infrastructure names.
```

The system should persist that mission and keep working:

- screen candidates,
- monitor news and earnings,
- classify risks and opportunities,
- update the mission feed,
- recommend watch/buy/wait/reject labels,
- avoid requiring the user to ask repeatedly.

## Event Engine

Needed next:

- concentration risk events,
- price breakout/drop events,
- earnings soon events,
- news/thesis change events,
- watchlist threshold events,
- mission update events,
- no-action confirmations.

The event engine should create structured DB events first. LLMs should enrich
only important events.

Events should feed both the Today surface and the evaluation layer. If the
assistant surfaced a warning, it should later know whether that warning was
useful.

## Data Quality Ladder

The current stack uses free/cheap data sources where possible. That is fine for
prototype speed, but data quality is a ceiling.

Current/free tier:

- Robinhood account/positions,
- yfinance prices/fundamentals,
- RSS/news headlines,
- simple technical signals.

Higher-quality future sources:

- real-time quotes,
- normalized fundamentals,
- SEC filings,
- 10-K/10-Q/8-K parsing,
- earnings call transcripts,
- analyst estimates/revisions,
- options skew/flow,
- sector/ETF benchmark data,
- corporate event calendar.

The architecture should keep these behind `brain/data/` interfaces so better
providers can replace weak ones without rewriting the assistant.

First paid/serious data upgrade should likely be fundamentals + filings +
earnings transcripts because they improve thesis quality more than just faster
prices.

## Deep Research Mode

Normal mode should be fast and cached. Deep research should be different:

- plan the research,
- pull multiple sources,
- compare bull/bear cases,
- self-critique,
- cite evidence,
- update stored thesis,
- write an audit trail to `agent_runs`.

Deep research is for important decisions, not every click.

## Recommendation Labels

Every recommendation should end with a clear action label:

- `BUY CANDIDATE`
- `WATCHLIST`
- `WAIT FOR PULLBACK`
- `HOLD`
- `TRIM`
- `EXIT REVIEW`
- `REJECT`
- `DO NOTHING`

No finance essays without a decision.

## Execution Philosophy

Manual execution first.

The system should generate trade cards and reasoning, but the user places
trades manually in Robinhood. Official MCP execution can be added later only
through a separate approved account/budget.

Execution-with-approval should only be considered after:

- the Shadow/evaluation layer shows calibrated edge,
- position sizing rules exist,
- max loss / max allocation guardrails exist,
- order review is human-approved,
- all actions are logged,
- only limit orders are allowed at first.

Do not automate execution before the research brain has earned trust.

## Roadmap Priority

1. Finish the DB spine:
   - use `agent_runs`,
   - make research/event/ticker memory fully DB-backed,
   - keep portfolio snapshots and source metadata clean.
2. Build evaluation:
   - turn Shadow into calibration, attribution, and benchmark-relative scoring.
3. Build living memory:
   - scheduled thesis revisits,
   - invalidation-trigger alerts,
   - stale research detection.
4. Add strategy missions:
   - persistent theme monitors that report back without prompting.
5. Improve data quality:
   - fundamentals, filings, earnings transcripts, estimates.
6. Add deep research mode:
   - multi-step, cited, self-critiqued analysis for high-stakes decisions.
7. Consider execution-with-approval only after measured edge exists.

## Quality Bar

The product should be:

- fast on first load,
- explicit about sources and timestamps,
- honest about uncertainty,
- visually restrained and polished,
- persistent through DB-backed state,
- proactive without being noisy,
- useful even when the right action is doing nothing,
- honest enough to show when it is wrong,
- measured enough to prove whether it is improving.
