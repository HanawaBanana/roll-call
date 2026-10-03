#!/usr/bin/env python3
"""Roll Call command line: seed, verify, export.

    python3 cli.py seed --demo           # build the demo history
    python3 cli.py verify                # walk the hash chain, print the first broken link
    python3 cli.py export csv -o out.csv # write an export
    python3 cli.py tamper --row-id 4     # red-team helper: corrupt one row on purpose

Every command takes an optional ``--db PATH`` (also read from ``$ROLLCALL_DB``);
the default is ``rollcall.sqlite3`` next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rollcall import anomalies, chain, db, demo, exports, service, util  # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rollcall.sqlite3")


def _connect(args):
    path = args.db or os.environ.get("ROLLCALL_DB") or DEFAULT_DB
    return db.connect(path), os.path.abspath(path)


def cmd_init(args) -> int:
    conn, path = _connect(args)
    conn.close()
    print("initialised %s" % path)
    return 0


def cmd_seed(args) -> int:
    conn, path = _connect(args)
    if args.demo:
        existing = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        if existing and not args.force:
            print("database already holds %d session(s); use --force to seed anyway" % existing)
            return 1
        report = demo.seed(conn)
        print(json.dumps(report, indent=2))
        print("\nseeded %s (%d accepted, %d duplicate, %d rejected)"
              % (path, report["accepted"], report["duplicates"], report["rejected"]))
    else:
        print("nothing to do: try 'seed --demo'")
    conn.close()
    return 0


def cmd_verify(args) -> int:
    conn, path = _connect(args)
    result = chain.verify(conn)
    conn.close()
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("chain OK: %d link(s) verified, head %s" % (result["checked"], result["head"]))
    else:
        broken = result["first_broken"]
        print("CHAIN BROKEN after %d verified link(s)" % result["checked"])
        print("  row        : %s" % (broken["attendance_id"] if broken["attendance_id"] else "(head pointer)"))
        print("  sequence   : %s" % broken["seq"])
        print("  reason     : %s" % broken["reason"])
        print("  detail     : %s" % broken["detail"])
        print("  stored hash: %s" % broken["stored_hash"])
        print("  recomputed : %s" % (broken["expected_hash"] or "-"))
    return 0 if result["ok"] else 2


def cmd_anomalies(args) -> int:
    conn, _path = _connect(args)
    findings = anomalies.detect_all(conn, args.session)
    conn.close()
    if args.json:
        print(json.dumps({"counts": anomalies.counts(findings), "anomalies": findings}, indent=2))
        return 0
    print("%d flag(s)" % len(findings))
    for f in findings:
        print("\n[%s] %s  (%s)" % (f["severity"].upper(), f["rule"], f["participant_ref"]))
        print("  %s" % f["reason"])
        print("  evidence rows: %s" % ", ".join("#%d" % i for i in f["evidence_ids"]))
    return 0


def cmd_summary(args) -> int:
    conn, _path = _connect(args)
    rows = service.summary_rows(conn, args.session)
    conn.close()
    print("%-8s %-22s %-10s %-8s %s" % ("ref", "name", "sessions", "entries", "flags"))
    for r in rows:
        print("%-8s %-22s %-10s %-8d %s" % (r["ref"], r["name"][:22],
                                            "%d/%d" % (r["sessions_attended"], r["sessions_total"]),
                                            r["entries"], ",".join(r["flags"]) or "-"))
    return 0


def cmd_export(args) -> int:
    conn, _path = _connect(args)
    kind = args.kind
    if kind == "csv":
        text = exports.attendance_csv(conn, args.session)
    elif kind == "summary":
        text = exports.summary_csv(conn, args.session)
    elif kind == "anomalies":
        text = exports.anomalies_csv(conn, args.session)
    elif kind == "json":
        text = exports.json_bundle(conn, args.session)
    else:
        print("unknown export %r (csv | summary | anomalies | json)" % kind)
        return 1
    conn.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("wrote %s (%d bytes)" % (args.out, len(text)))
    else:
        sys.stdout.write(text)
    return 0


def cmd_tamper(args) -> int:
    conn, _path = _connect(args)
    row = conn.execute("SELECT id FROM attendance ORDER BY seq ASC LIMIT 1 OFFSET ?",
                       (max(0, args.offset),)).fetchone()
    if row is None:
        print("no attendance rows to tamper with")
        return 1
    row_id = args.row_id or row["id"]
    out = chain.tamper(conn, row_id, args.field, args.value)
    conn.close()
    print("tampered row #%d: %s = %r -> %r (the hash chain was NOT updated)"
          % (out["attendance_id"], out["field"], out["before"], out["after"]))
    print("now run: python3 cli.py verify")
    return 0


def cmd_code(args) -> int:
    conn, _path = _connect(args)
    info = service.current_code(conn, args.session)
    session = service.get_session(conn, info["session_id"])
    conn.close()
    print("session #%d %s (%s)" % (session["id"], session["label"], session["location"]))
    print("  code now      : %s   (rotation step %d, %d s left)"
          % (info["current"], info["step"], info["seconds_left"]))
    print("  previous code : %s   (grace window)" % info["previous"])
    return 0


def cmd_sessions(args) -> int:
    conn, _path = _connect(args)
    rows = service.list_sessions(conn)
    conn.close()
    for r in rows:
        print("#%-3d %-38s %-22s %s -> %s  entries=%d%s"
              % (r["id"], r["label"][:38], r["location"][:22], r["opens_at"], r["closes_at"],
                 r["entries"], "  CLOSED" if r["closed_at"] else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="Roll Call command line")
    parser.add_argument("--db", help="sqlite file (default: rollcall.sqlite3 next to cli.py)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create the database file and schema")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("seed", help="seed demo data")
    p.add_argument("--demo", action="store_true", help="build the standard demo history")
    p.add_argument("--force", action="store_true", help="seed even if sessions exist")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("verify", help="verify the hash chain")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("anomalies", help="run the anomaly rules")
    p.add_argument("--session", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_anomalies)

    p = sub.add_parser("summary", help="per-participant attendance summary")
    p.add_argument("--session", type=int)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("export", help="export attendance")
    p.add_argument("kind", choices=["csv", "summary", "anomalies", "json"])
    p.add_argument("--session", type=int)
    p.add_argument("-o", "--out")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("tamper", help="red-team helper: corrupt one row and leave the chain stale")
    p.add_argument("--row-id", type=int)
    p.add_argument("--offset", type=int, default=2)
    p.add_argument("--field", default="checked_in_at")
    p.add_argument("--value")
    p.set_defaults(func=cmd_tamper)

    p = sub.add_parser("code", help="print the live rotating code")
    p.add_argument("--session", type=int)
    p.set_defaults(func=cmd_code)

    p = sub.add_parser("sessions", help="list sessions")
    p.set_defaults(func=cmd_sessions)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
