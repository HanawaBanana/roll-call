#!/usr/bin/env python3
"""Roll Call - start the offline check-in and attendance app.

    python3 app.py                     # http://127.0.0.1:8080, state in ./rollcall.sqlite3
    python3 app.py --port 8782         # the port the demo video uses
    python3 app.py --demo --db /tmp/x.sqlite3

Standard library only, no accounts, no network.  The UI and the JSON API are the
same process; the state is one sqlite file you can copy to a USB stick.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rollcall import db, demo, web  # noqa: E402

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def default_db() -> str:
    return os.environ.get("ROLLCALL_DB") or os.path.join(PROJECT_DIR, "rollcall.sqlite3")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roll Call - offline check-in for a small class")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    parser.add_argument("--db", default=None, help="sqlite file (default ./rollcall.sqlite3)")
    parser.add_argument("--demo", action="store_true", help="seed the demo history if the database is empty")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    db_path = args.db or default_db()

    conn = db.connect(db_path)
    try:
        empty = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
        if args.demo and empty:
            report = demo.seed(conn)
            print("seeded demo history: sessions %s, %d accepted check-ins"
                  % (report["sessions"], report["accepted"]))
    finally:
        conn.close()

    web.serve(db_path, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
