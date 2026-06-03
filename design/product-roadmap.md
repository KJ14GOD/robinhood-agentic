# Product Design Roadmap

This roadmap sits beside `PRODUCT_VISION.md`. That file defines the product brain and long-term system. This file defines how the product should *present* that intelligence so it feels production-grade instead of prototype-grade.

## Product Meaning

The product is not "an AI chatbot for stocks" and not "Robinhood with extra cards."

It is a personal market operating system:

```text
It watches the portfolio, remembers the thesis, notices what changed, measures whether it was right, and tells the user what deserves attention.
```

The UI should feel like an instrument panel for judgment, not a dashboard of widgets.

## Brand Direction

The current `BRAIN + green dot + ROBINHOOD` header is useful for a prototype, but it reads as implementation status. In production, the brand should mean something deeper.

### Working Name Direction

Possible product names:

- `Signal`
- `Thesis`
- `Conviction`
- `Ledger`
- `Vector`
- `North`
- `Axis`

The strongest direction is probably `Signal`.

Why:

- It implies separating signal from noise.
- It fits alerts, research, memory, and scorecard.
- It is serious without sounding like a hedge fund cosplay.
- It avoids overclaiming autonomy or guaranteed returns.

### Logo Meaning

Avoid:

- green status dots as brand marks,
- literal brain icons,
- robot/AI sparkles,
- finance candlestick clichés,
- Robinhood-like branding.

Preferred mark:

- a quiet directional glyph,
- a folded paper/arrow/signal path,
- one shape that suggests "reading the market and pointing attention,"
- black/ink primary, with color only as data state.

Meaning:

```text
The mark is not "AI is alive."
The mark is "attention is being directed."
```

Connection state should be secondary:

```text
Signal        Robinhood read-only
```

not:

```text
green dot BRAIN ROBINHOOD
```

## Product Shell

The shell needs to become calmer and more intentional.

### Top Bar

Left:

- product mark,
- product name,
- current data connection as a small status pill.

Center:

- global command/search: ticker, question, research request, strategy mission.

Right:

- portfolio value,
- sync state,
- notifications,
- settings.

### Navigation

The current tabs should move toward user jobs, not internal systems.

Recommended final IA:

```text
Today
Activity
Portfolio
Memory
Scorecard
Settings
```

Optional later:

```text
Research
```

Only add `Research` if Memory becomes too crowded with missions/deep dives. Do not add tabs for every engine.

## Surface Roadmap

### 1. Today

Current equivalent: `Brain`.

Purpose:

```text
What changed? What matters? What can I ignore? What is the assistant watching?
```

Production layout:

- top state-of-portfolio read,
- urgent alerts,
- no-action confirmation,
- research queue,
- watchlist/mission updates,
- assistant composer.

Example:

```text
Today
No forced action.
Your book is still concentrated in high-beta AI/space, but no thesis invalidation fired since the last scan.

Needs attention:
1. RKLB: concentration above comfort line.
2. APLD: AI capex thesis still active, wait for pullback.
3. SOFI: below 200d, do not chase bounce.
```

Design principle:

Today should not be a feed of everything. It should be an edited front page.

### 2. Activity

Current equivalent: `Activity`.

Purpose:

```text
The audit trail of what the system observed and why it surfaced it.
```

Production layout:

- chronological event stream,
- filters: all, alerts, thesis, monitor, briefings, deep research,
- severity,
- source engine,
- linked evidence,
- "why this appeared" disclosure.

Activity is not for beauty. It is for trust.

### 3. Portfolio

Current equivalent: `Portfolio`.

Purpose:

```text
Account truth plus portfolio structure.
```

Production layout:

- Robinhood-like chart and position rail,
- account value and buying power,
- holdings list,
- allocation,
- factor exposure,
- concentration map,
- stock click-through chart state.

Important:

The AI should not take over this page. Portfolio is first about inspecting the book. The AI layer should appear as a quiet structural read below or beside the chart.

### 4. Memory

Current equivalent: `Memory`.

Purpose:

```text
Durable investment case files.
```

Production layout:

- ticker case file list,
- selected ticker detail,
- latest thesis,
- why tracking,
- invalidation rule,
- support/pressure evidence ledger,
- thesis status,
- refresh thesis / deep research.

This page must clearly separate:

- `Latest analysis`: what the brain said most recently.
- `Stored thesis`: the durable belief.
- `Watch reason`: why the system is tracking it.
- `Evidence ledger`: how the thesis changed over time.

If those stay visually separate, the DB tables stop feeling like duplicates.

### 5. Scorecard

Current equivalent: `Shadow`.

Purpose:

```text
Prove whether the assistant is useful.
```

Production layout:

- hit rate,
- alpha vs benchmark,
- max drawdown,
- calibration by conviction,
- performance by action label,
- performance by source engine,
- latest recommendations ledger.

This is the page that makes the product serious. Without it, the assistant can sound smart but cannot earn trust.

### 6. Settings

Current equivalent: `Profile`.

Purpose:

```text
Guardrails, sources, profile, and automation controls.
```

Production layout:

- investor profile,
- risk limits,
- strategy preferences,
- data source status,
- connected brokerage accounts,
- login/OAuth and account permissions,
- notification cadence,
- LLM spend limits,
- execution mode.

Profile should become less like a form and more like a control plane.

## Account And Brokerage Roadmap

Production `Signal` should not be tied to one brokerage in the product identity. Robinhood can be the first connection, but the app should be designed as a source-aware portfolio operating system.

### Account Layer

Eventually the app needs:

- user login,
- OAuth or secure broker connection flows where providers support it,
- encrypted token storage,
- account permissions,
- multiple brokerage/account connections,
- account-level sync health,
- account selection / aggregation,
- read-only vs execution-capable modes.

The product shell should say:

```text
Signal
Connected: Robinhood read-only
```

or:

```text
Signal
3 accounts connected
```

not:

```text
Robinhood Brain
```

### Broker-Agnostic Mental Model

Brokerages are data sources. They should not define the product.

The durable objects should be:

- user,
- connected account,
- portfolio snapshot,
- position,
- thesis,
- event,
- recommendation,
- scorecard result.

That keeps the product expandable to:

- Robinhood,
- Schwab,
- Fidelity,
- Coinbase,
- Alpaca,
- Interactive Brokers,
- manual/imported accounts,
- future execution-with-approval accounts.

Execution should stay separated from intelligence:

```text
Read account -> reason -> recommend -> user approves/manual executes
```

Only after the scorecard proves edge should execution-with-approval become part of the product.

## Intelligence Display Pattern

Every generated insight should follow a consistent shape:

```text
Decision
Evidence
Risk
What would change the call
Action label
Source / freshness
```

Example:

```text
WAIT FOR PULLBACK
APLD thesis remains intact, but price is extended versus the entry risk.
Evidence: hyperscaler lease supports demand; RSI and recent move raise chase risk.
Breaks if: AI capex cycle cools or financing slips.
Action: no trade today. Alert under target entry.
Source: thesis refresh, updated 11:42 AM.
```

This avoids essay-like AI output and keeps the system decisive.

## Visual System

The visual system should be restrained.

### Palette

Use:

- warm off-white background,
- black/ink text,
- soft border lines,
- muted gray metadata,
- green/red only for market movement,
- blue or violet for assistant/research state,
- amber for caution/risk.

Avoid:

- green as the whole brand,
- large black boxes everywhere,
- saturated gradient blobs,
- colorful badge overload,
- card stacks inside card stacks.

### Component Style

Use:

- unboxed page sections where possible,
- cards only for repeated items or real frames,
- thin borders instead of heavy shadows,
- stable two-column layouts,
- dense but readable financial data,
- large chart surfaces,
- compact metadata.

## Technical Roadmap For UI

### Phase 1: Clarify Current App

Goal: improve the existing static frontend without a rewrite.

- Rename Brain -> Today.
- Rename Shadow -> Scorecard.
- Remove green status-dot brand treatment.
- Add a real product shell.
- Keep Portfolio two-column.
- Make Today the edited front page.
- Make Activity the raw audit trail.
- Make Memory case-file-first.

### Phase 2: Component System

Goal: stop hand-building one-off UI.

- Define design tokens.
- Define reusable components:
  - app shell,
  - command bar,
  - insight card,
  - event row,
  - ticker case file,
  - score metric,
  - chart shell,
  - source pill.
- Replace tab-specific styling with shared primitives.

### Phase 3: Frontend Migration

Goal: move from static HTML/JS to a production frontend.

Recommended:

- Next.js or React + Vite,
- TypeScript,
- TanStack Query,
- generated API types from FastAPI/OpenAPI,
- lightweight-charts or visx,
- Radix/shadcn primitives or a custom minimal component system.

Keep the FastAPI backend initially. Do not rewrite the brain and frontend at the same time.

### Phase 4: Productized Proactivity

Goal: make it feel alive without becoming noisy.

- Today pre-warmed by scheduled scans.
- No-action confirmations.
- Notification inbox.
- Mission updates.
- Thesis invalidation alerts.
- Scorecard updates.
- User-controllable cadence.

### Phase 5: Trust Layer

Goal: earn action.

- public/internal scorecard,
- calibrated conviction,
- source attribution,
- backtest/replay where possible,
- recommendation lineage from event -> research -> action -> outcome.

## North-Star Screen Order

When a user opens the app, the mental flow should be:

```text
Today:
Do I need to care right now?

Portfolio:
What do I own and how exposed am I?

Memory:
What do we believe and what would break it?

Scorecard:
Is this assistant actually good?

Activity:
Show me the audit trail.

Settings:
Control sources, risk, and cadence.
```

That order should guide design decisions.

## Guiding Rule

Do not show the user the machinery unless they asked for it.

The engines can be complex:

- monitor,
- memory,
- briefing,
- discovery,
- deep research,
- scorecard,
- missions.

The product should feel simple:

```text
Here is what matters.
Here is why.
Here is what I am watching.
Here is how I know whether I am right.
```
