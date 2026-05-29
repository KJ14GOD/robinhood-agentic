#!/usr/bin/env python3
"""Terminal access to the brain, for quick checks without the dashboard.

  python cli.py analyze NVDA
  python cli.py discover --flavor stable
  python cli.py digest
  python cli.py briefing --kind morning
  python cli.py score
  python cli.py refresh
  python cli.py db-init
  python cli.py db-stats
  python cli.py ask "give me 3 stable dividend ideas"
"""
from __future__ import annotations

import argparse
import json

from brain import orchestrator as brain
from brain.db.session import db_session
from brain.db import models as db_models


def main() -> None:
    ap = argparse.ArgumentParser(description="Stock research brain CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze"); a.add_argument("ticker")
    d = sub.add_parser("discover"); d.add_argument("--flavor", default="any",
                                                   choices=["any", "stable", "volatile"])
    d.add_argument("--n", type=int, default=5)
    sub.add_parser("digest")
    b = sub.add_parser("briefing"); b.add_argument("--kind", default="manual",
                                                   choices=["morning", "evening", "manual"])
    sub.add_parser("score")
    sub.add_parser("refresh")
    sub.add_parser("db-init")
    sub.add_parser("db-stats")
    sub.add_parser("portfolio")
    sub.add_parser("memory")
    k = sub.add_parser("ask"); k.add_argument("message")

    args = ap.parse_args()
    if args.cmd == "analyze":
        print(brain.analyze(args.ticker).model_dump_json(indent=2))
    elif args.cmd == "discover":
        print(brain.discover(flavor=args.flavor, top_n=args.n).model_dump_json(indent=2))
    elif args.cmd == "digest":
        print(brain.daily_digest().model_dump_json(indent=2))
    elif args.cmd == "briefing":
        print(brain.create_briefing(args.kind).model_dump_json(indent=2))
    elif args.cmd == "score":
        print(json.dumps(brain.scoreboard(), indent=2, default=str))
    elif args.cmd == "refresh":
        print(brain.refresh_live_state().model_dump_json(indent=2))
    elif args.cmd == "db-init":
        brain.init_database()
        print("database initialized")
    elif args.cmd == "db-stats":
        brain.init_database()
        tables = [
            db_models.PortfolioSnapshot,
            db_models.PositionSnapshot,
            db_models.QuoteSnapshot,
            db_models.ThesisRecord,
            db_models.WatchlistItemRecord,
            db_models.BriefingRecord,
            db_models.PriceAlertRecord,
            db_models.ResearchEventRecord,
            db_models.TickerResearchRecord,
            db_models.AgentRun,
        ]
        with db_session() as session:
            stats = {t.__tablename__: session.query(t).count() for t in tables}
        print(json.dumps(stats, indent=2))
    elif args.cmd == "portfolio":
        print(brain.portfolio().model_dump_json(indent=2))
    elif args.cmd == "memory":
        print(brain.get_research_state().model_dump_json(indent=2))
    elif args.cmd == "ask":
        for ev in brain.chat_stream(args.message):
            t = ev["type"]
            if t == "tool":
                print(f"  🔧 {ev['name']}({ev['input']})")
            elif t == "tool_result":
                print(f"     ↳ {ev['summary'][:120]}")
            elif t == "note":
                print(f"  💭 {ev['text']}")
            elif t == "answer":
                print("\n" + ev["text"])


if __name__ == "__main__":
    main()
