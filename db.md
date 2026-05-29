whats stored in the db 

quote snapshots - per ticker history

  This is just a flat, append-only price log. Every refresh cycle (~120s), for each stock you hold, one row gets written:

  ticker | price  | source                          | captured_at
  APLD   | 47.55  | Robinhood API 24_7 historicals  | 2026-05-29 12:58
  APLD   | 47.61  | Robinhood API 24_7 historicals  | 2026-05-29 13:00
  NVDA   | 131.2  | Robinhood API 24_7 historicals  | 2026-05-29 12:58

   Say you hold 8 stocks. Each ~2-minute cycle inserts:

  ┌─────────────────────┬─────────────────────┬───────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │        Table        │     Rows added      │                                                   Contents                                                    │
  ├─────────────────────┼─────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ portfolio_snapshots │ 1                   │ account header: total value, cash, buying power, reported equity, timestamp                                   │
  ├─────────────────────┼─────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ position_snapshots  │ 8 (one per holding) │ per stock: qty, avg cost, current price, market value, weight — all linked to that 1 snapshot via snapshot_id │
  ├─────────────────────┼─────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ quote_snapshots     │ 8 (one per holding) │ per stock: ticker, price, source, timestamp — standalone, no link                                             │
  └─────────────────────┴─────────────────────┴───────────────────────────────────────────────────────────────────────────────────────────────────────────────┘