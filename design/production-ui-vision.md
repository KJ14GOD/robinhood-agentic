# Production UI Vision

This is a first visual direction for moving the product away from the current prototype shell and toward a real portfolio intelligence product.

## Product Shell

The current `BRAIN + ROBINHOOD` header reads like a prototype because it exposes implementation status as brand identity. A production shell should separate those:

- Product name / wordmark: neutral, quiet, not a green status dot.
- Data connection: shown as a small operational status, e.g. `Robinhood read-only`.
- Command entry: persistent search / ask box for ticker jumps, research, and portfolio questions.
- Tabs: fewer conceptual overlaps, clearer user jobs.

## Tab Direction

### Brain -> Today

Purpose: the first thing the user sees.

It should answer: "What changed, what matters, what should I do, and what is the assistant watching?"

Surface:
- one state-of-portfolio call
- urgent alerts
- no-action confirmations
- top research queue
- assistant composer

### Activity

Purpose: audit trail.

It should not feel like another summary tab. It is the raw, timestamped record of signals and judgments.

Surface:
- deterministic signals
- LLM judgments
- thesis updates
- source labels
- severity
- event IDs / citations later

### Portfolio

Purpose: inspect the book.

This should stay closest to Robinhood's information architecture: big chart and account context on the left, holdings rail on the right. The AI layer should be integrated below the chart as factor exposure and risk, not as a huge separate dashboard card.

Surface:
- portfolio chart
- position list
- allocation
- factor exposure
- concentration warnings
- click a stock to switch chart context

### Memory

Purpose: durable case files.

Memory should not look like another watchlist. It should show the evolving belief for each ticker.

Surface:
- ticker case list
- latest thesis
- watch reason
- invalidation rules
- evidence ledger: strengthens / weakens
- thesis status: active / review / broken

### Shadow -> Scorecard

Purpose: trust.

This is how the assistant proves whether it is useful. The user should be able to see if recommendations beat benchmarks and whether confidence is calibrated.

Surface:
- hit rate
- alpha vs SPY / QQQ / sector ETF
- max drawdown
- calibration by conviction
- source/engine attribution
- latest calls ledger

### Profile -> Settings and Guardrails

Purpose: production control plane.

The current profile is useful, but production needs explicit controls.

Surface:
- risk appetite
- max position sizes
- blocked sectors / favored themes
- notification cadence
- data source status
- LLM cost limits
- execution mode: manual / approval-only / off

## Migration Direction

Do not rewrite the brain first. Keep the Python backend, DB, Robinhood integration, event engine, memory, and scorecard. Modernize the frontend around it.

Recommended future stack:

- React or Next.js frontend
- TypeScript
- TanStack Query for API caching / polling
- Component primitives: shadcn/Radix or a custom minimal system
- Charting: lightweight-charts or visx
- API contract typed with Pydantic -> OpenAPI -> generated TS types
- Keep current FastAPI backend initially

The important migration is not "HTML to React" by itself. It is moving from one-off screens to a consistent app shell, design tokens, reusable components, typed API state, and clearer product surfaces.
