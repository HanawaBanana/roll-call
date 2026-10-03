"""CSV / JSON exports: raw attendance, per-participant summary, full JSON bundle."""

from __future__ import annotations

import csv
import io
import json

from . import anomalies, chain, db as _db, service, util

ATTENDANCE_COLUMNS = (
    "attendance_id", "seq", "session_id", "session_label", "session_location",
    "participant_ref", "participant_name", "kind", "checked_in_at", "recorded_at",
    "code_used", "code_step", "session_step", "source", "prev_hash", "entry_hash",
)

SUMMARY_COLUMNS = (
    "participant_ref", "participant_name", "sessions_attended", "sessions_total",
    "entries", "duplicates", "first_checkin", "last_checkin", "flags",
)

ANOMALY_COLUMNS = ("rule", "severity", "session_id", "participant_ref", "participant_name",
                   "reason", "evidence_ids")


def _csv(columns, rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buf.getvalue()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(str(v) for v in value)
    return value


def attendance_rows(conn, session_id=None) -> list:
    sql = anomalies.SELECT_ROWS
    args = ()
    if session_id is not None:
        sql += " WHERE a.session_id = ?"
        args = (int(session_id),)
    raw = _db.many(conn, sql + " ORDER BY a.seq ASC", args)
    out = []
    for r in raw:
        out.append({
            "attendance_id": r["id"], "seq": r["seq"], "session_id": r["session_id"],
            "session_label": r["session_label"], "session_location": r["session_location"],
            "participant_ref": r["participant_ref"], "participant_name": r["participant_name"],
            "kind": r["kind"], "checked_in_at": r["checked_in_at"],
            "recorded_at": r["recorded_at"], "code_used": r["code_used"],
            "code_step": r["code_step"], "session_step": r["session_step"],
            "source": r["source"], "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"],
        })
    return out


def attendance_csv(conn, session_id=None) -> str:
    return _csv(ATTENDANCE_COLUMNS, attendance_rows(conn, session_id))


def summary_csv(conn, session_id=None) -> str:
    rows = []
    for row in service.summary_rows(conn, session_id):
        rows.append({
            "participant_ref": row["ref"], "participant_name": row["name"],
            "sessions_attended": row["sessions_attended"], "sessions_total": row["sessions_total"],
            "entries": row["entries"], "duplicates": row["duplicates"],
            "first_checkin": row["first_checkin"], "last_checkin": row["last_checkin"],
            "flags": row["flags"],
        })
    return _csv(SUMMARY_COLUMNS, rows)


def anomalies_csv(conn, session_id=None) -> str:
    rows = []
    for f in anomalies.detect_all(conn, session_id):
        rows.append({
            "rule": f["rule"], "severity": f["severity"], "session_id": f["session_id"],
            "participant_ref": f["participant_ref"], "participant_name": f["participant_name"],
            "reason": f["reason"], "evidence_ids": f["evidence_ids"],
        })
    return _csv(ANOMALY_COLUMNS, rows)


def json_bundle(conn, session_id=None) -> str:
    """Machine readable export of everything the log can say."""
    session = service.pick_session(conn, session_id)
    sessions = _db.many(conn, "SELECT * FROM sessions ORDER BY id ASC")
    for s in sessions:
        s.pop("secret", None)
    sessions = [s for s in sessions if session_id is None or s["id"] == int(session_id)]
    payload = {
        "schema": "rollcall/v1",
        "generated_at": util.iso(util.now_utc()),
        "scope_session_id": int(session_id) if session_id is not None else None,
        "sessions": sessions,
        "participants": _db.many(conn, "SELECT * FROM participants ORDER BY ref ASC"),
        "attendance": attendance_rows(conn, session_id),
        "summary": service.summary_rows(conn, session_id),
        "anomalies": anomalies.detect_all(conn, session_id),
        "chain": chain.verify(conn),
        "chain_head": chain.head(conn),
        "totals": {
            "entries": _db.one(conn, "SELECT COUNT(*) AS n FROM attendance")["n"],
            "participants": _db.one(conn, "SELECT COUNT(*) AS n FROM participants")["n"],
            "sessions": _db.one(conn, "SELECT COUNT(*) AS n FROM sessions")["n"],
        },
    }
    return json.dumps(payload, indent=2, sort_keys=False)
