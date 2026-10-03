"""Application logic: sessions, check-ins, roster, summaries, UI state blob.

The service layer is the only place that writes to the database, which keeps the
hash chain in one code path.  Every function that depends on "now" takes an
optional ``now`` argument: the web API always passes the real clock, the CLI uses
it to seed a realistic history and the tests use it to be deterministic.
"""

from __future__ import annotations

from . import anomalies, chain, codes, db as _db, util


class ServiceError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- sessions

def create_session(conn, label, location="", minutes=60, step_seconds=None, opens_at=None,
                   lat=None, lon=None, now=None) -> dict:
    """Open a session.  ``opens_at`` may be backdated (substitute facilitator)."""
    at = now or util.now_utc()
    label = (label or "").strip()
    if not label:
        raise ServiceError("invalid_session", "a session needs a label")
    minutes = int(60 if minutes is None else minutes)
    if minutes < 1 or minutes > 24 * 60:
        raise ServiceError("invalid_session", "minutes must be between 1 and 1440")
    step = int(codes.STEP_SECONDS_DEFAULT if step_seconds is None else step_seconds)
    if step < 5 or step > 3600:
        raise ServiceError("invalid_session", "step_seconds must be between 5 and 3600")
    opens = util.parse_iso(opens_at) if opens_at else at
    closes = util.plus(opens, minutes=minutes)
    conn.execute(
        """INSERT INTO sessions (label, location, lat, lon, opens_at, closes_at, step_seconds,
                                 secret, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (label, (location or "").strip(), lat, lon, util.iso(opens), util.iso(closes), step,
         util.new_secret(), util.iso(at)))
    row = _db.one(conn, "SELECT * FROM sessions WHERE id = last_insert_rowid()")
    return row


def get_session(conn, session_id) -> dict:
    row = _db.one(conn, "SELECT * FROM sessions WHERE id = ?", (int(session_id),))
    if row is None:
        raise ServiceError("unknown_session", "no session with id %s" % session_id)
    return row


def session_counts(conn) -> dict:
    rows = _db.many(conn, "SELECT session_id, COUNT(*) AS n FROM attendance GROUP BY session_id")
    return {r["session_id"]: r["n"] for r in rows}


def list_sessions(conn, limit=100) -> list:
    counts = session_counts(conn)
    rows = _db.many(conn, "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (int(limit),))
    for r in rows:
        r.pop("secret", None)
        r["entries"] = counts.get(r["id"], 0)
    return rows


def close_session(conn, session_id, now=None) -> dict:
    at = now or util.now_utc()
    get_session(conn, session_id)
    conn.execute("UPDATE sessions SET closed_at = ? WHERE id = ?", (util.iso(at), int(session_id)))
    return get_session(conn, session_id)


def pick_session(conn, session_id=None, now=None) -> dict:
    """The requested session, else the newest open one, else the newest one."""
    at = now or util.now_utc()
    if session_id not in (None, "", "latest"):
        return get_session(conn, session_id)
    row = _db.one(conn, """SELECT * FROM sessions
                           WHERE closed_at IS NULL AND opens_at <= ? AND closes_at >= ?
                           ORDER BY id DESC LIMIT 1""", (util.iso(at), util.iso(at)))
    if row is None:
        row = _db.one(conn, "SELECT * FROM sessions ORDER BY id DESC LIMIT 1")
    return row


# --------------------------------------------------------------------------- codes

def code_info(session, now=None) -> dict:
    """The code on the facilitator's screen right now, plus the countdown."""
    at = now or util.now_utc()
    epoch = util.epoch(at)
    step_seconds = int(session["step_seconds"])
    step = codes.step_for(epoch, step_seconds)
    left = codes.seconds_left(epoch, step_seconds)
    start, end = codes.step_window(step, step_seconds)
    return {
        "session_id": session["id"],
        "current": codes.derive_code(session["secret"], session["id"], step),
        "previous": codes.derive_code(session["secret"], session["id"], step - 1),
        "step": step,
        "step_seconds": step_seconds,
        "grace_steps": codes.GRACE_STEPS,
        "seconds_left": left,
        "expires_at_ms": int(end * 1000),
        "server_ms": int(epoch * 1000),
    }


# --------------------------------------------------------------------------- check-in

def current_code(conn, session_id=None, now=None) -> dict:
    at = now or util.now_utc()
    session = pick_session(conn, session_id, at)
    if session is None:
        raise ServiceError("no_session", "no session has been opened yet")
    return code_info(session, at)


def _participant_by_ref(conn, ref) -> dict:
    if not ref:
        return None
    return _db.one(conn, "SELECT * FROM participants WHERE ref = ?",
                   (str(ref).strip().upper(),))


def ensure_participant(conn, ref, name=None, now=None) -> dict:
    """Look up a participant by reference, creating them on first sight."""
    at = now or util.now_utc()
    ref = (ref or "").strip().upper()
    if not ref:
        raise ServiceError("invalid_participant", "a check-in needs a participant reference")
    if len(ref) > 32:
        raise ServiceError("invalid_participant", "participant reference is too long")
    found = _participant_by_ref(conn, ref)
    if found is not None:
        return found
    name = (name or "").strip()
    if not name:
        raise ServiceError("invalid_participant",
                           "first check-in for %s needs a name" % ref)
    conn.execute("INSERT INTO participants (ref, name, created_at) VALUES (?,?,?)",
                 (ref, name[:64], util.iso(at)))
    return _db.one(conn, "SELECT * FROM participants WHERE ref = ?", (ref,))


def _reject(conn, session_id, ref, code, reason, at) -> None:
    conn.execute("""INSERT INTO rejected_attempts (session_id, participant_ref, code, reason, at)
                    VALUES (?,?,?,?,?)""",
                 (session_id, (ref or "")[:32], (code or "")[:16], reason, util.iso(at)))


def check_in(conn, session_id, ref, name=None, code=None, now=None, source="web") -> dict:
    """Record one check-in attempt.  Returns accepted / duplicate / rejected."""
    at = now or util.now_utc()
    at_iso = util.iso(at)
    session = get_session(conn, session_id)

    result = {
        "status": "rejected", "session_id": session["id"], "session_label": session["label"],
        "participant_ref": (ref or "").strip().upper(), "at": at_iso, "source": source,
    }

    if session["closed_at"]:
        result["reason"] = "session_closed"
        result["message"] = "session %d was closed at %s" % (session["id"], session["closed_at"])
        _reject(conn, session["id"], ref, code, "session_closed", at)
        return result

    if not code:
        result["reason"] = "missing_code"
        result["message"] = "the rotating code from the facilitator screen is required"
        _reject(conn, session["id"], ref, code, "missing_code", at)
        return result

    check = codes.check_code(session["secret"], session["id"], code, util.epoch(at),
                             session["step_seconds"])
    result["code"] = check.as_dict()
    if not check.accepted:
        result["reason"] = check.status
        result["message"] = check.message
        _reject(conn, session["id"], ref, code, check.status, at)
        return result

    try:
        participant = ensure_participant(conn, ref, name, at)
    except ServiceError as exc:
        result["reason"] = exc.code
        result["message"] = exc.message
        _reject(conn, session["id"], ref, code, exc.code, at)
        return result

    previous = _db.one(conn, """SELECT COUNT(*) AS n FROM attendance
                                WHERE session_id = ? AND participant_id = ?""",
                       (session["id"], participant["id"]))["n"]
    kind = "duplicate" if previous else "checkin"

    entry = chain.append(
        conn, session_id=session["id"], participant_id=participant["id"], kind=kind,
        code_used=code.strip().upper(), code_step=check.code_step,
        session_step=check.session_step, checked_in_at=at_iso, recorded_at=at_iso,
        source=source, note="late code accepted in grace window" if check.stale else "")

    result.update({
        "status": "duplicate" if kind == "duplicate" else "accepted",
        "reason": "duplicate_check_in" if kind == "duplicate" else "ok",
        "message": ("%s was already checked in to this session - the extra entry is kept and "
                    "flagged" % participant["name"]) if kind == "duplicate"
                   else "check-in recorded",
        "attendance_id": entry["id"],
        "seq": entry["seq"],
        "entry_hash": entry["entry_hash"],
        "prev_hash": entry["prev_hash"],
        "kind": kind,
        "stale_code": check.stale,
        "participant": participant,
        "session_step": check.session_step,
        "code_step": check.code_step,
    })
    return result


# --------------------------------------------------------------------------- views

def roster(conn, session_id=None, now=None) -> list:
    """Every known participant with their status for one session."""
    at = now or util.now_utc()
    session = pick_session(conn, session_id, at)
    if session is None:
        return []
    entries = _db.many(conn, """SELECT participant_id, COUNT(*) AS entries,
                                       MIN(checked_in_at) AS first_checkin,
                                       MAX(checked_in_at) AS last_checkin,
                                       SUM(CASE WHEN kind = 'duplicate' THEN 1 ELSE 0 END) AS duplicates
                                FROM attendance WHERE session_id = ?
                                GROUP BY participant_id""", (session["id"],))
    by_pid = {e["participant_id"]: e for e in entries}
    flags = {}
    for finding in anomalies.detect_all(conn, session["id"]):
        for pid in {row["participant_id"] for row in finding["evidence"]}:
            flags.setdefault(pid, [])
            if finding["rule"] not in flags[pid]:
                flags[pid].append(finding["rule"])

    people = _db.many(conn, "SELECT * FROM participants ORDER BY ref ASC")
    out = []
    for p in people:
        e = by_pid.get(p["id"], {})
        out.append({
            "id": p["id"], "ref": p["ref"], "name": p["name"],
            "present": bool(e),
            "entries": e.get("entries", 0),
            "duplicates": e.get("duplicates", 0) or 0,
            "first_checkin": e.get("first_checkin"),
            "last_checkin": e.get("last_checkin"),
            "flags": flags.get(p["id"], []),
        })
    out.sort(key=lambda r: (not r["present"], r["first_checkin"] or "9", r["ref"]))
    return out


def summary_rows(conn, session_id=None) -> list:
    """Per-participant attendance summary (drives summary.csv and the UI table)."""
    if session_id is None:
        total_sessions = _db.one(conn, "SELECT COUNT(*) AS n FROM sessions")["n"]
    else:
        total_sessions = 1
    sql = """SELECT a.participant_id, a.session_id, a.checked_in_at, a.kind
             FROM attendance a"""
    args = ()
    if session_id is not None:
        sql += " WHERE a.session_id = ?"
        args = (int(session_id),)
    rows = _db.many(conn, sql, args)

    people = {p["id"]: p for p in _db.many(conn, "SELECT * FROM participants")}
    agg = {}
    for r in rows:
        a = agg.setdefault(r["participant_id"], {
            "sessions": set(), "entries": 0, "duplicates": 0,
            "first_checkin": None, "last_checkin": None})
        a["sessions"].add(r["session_id"])
        a["entries"] += 1
        if r["kind"] == "duplicate":
            a["duplicates"] += 1
        if a["first_checkin"] is None or r["checked_in_at"] < a["first_checkin"]:
            a["first_checkin"] = r["checked_in_at"]
        if a["last_checkin"] is None or r["checked_in_at"] > a["last_checkin"]:
            a["last_checkin"] = r["checked_in_at"]

    findings = anomalies.detect_all(conn, session_id)
    flags_by_pid = {}
    for f in findings:
        flags_by_pid.setdefault(f["participant_id"], set()).add(f["rule"])

    out = []
    for pid, p in people.items():
        a = agg.get(pid)
        out.append({
            "participant_id": pid, "ref": p["ref"], "name": p["name"],
            "sessions_attended": len(a["sessions"]) if a else 0,
            "sessions_total": int(total_sessions),
            "entries": a["entries"] if a else 0,
            "duplicates": a["duplicates"] if a else 0,
            "first_checkin": a["first_checkin"] if a else None,
            "last_checkin": a["last_checkin"] if a else None,
            "flags": sorted(flags_by_pid.get(pid, set())),
        })
    out.sort(key=lambda r: (-r["sessions_attended"], r["ref"]))
    return out


def recent_entries(conn, limit=8, session_id=None) -> list:
    sql = anomalies.SELECT_ROWS
    args = ()
    if session_id is not None:
        sql += " WHERE a.session_id = ?"
        args = (int(session_id),)
    sql += " ORDER BY a.seq DESC LIMIT ?"
    args = tuple(args) + (int(limit),)
    rows = _db.many(conn, sql, args)
    keys = anomalies.EVIDENCE_FIELDS + ("recorded_at", "note", "prev_hash")
    return [{k: r.get(k) for k in keys} for r in rows]


def state(conn, session_id=None, view="session", now=None) -> dict:
    """Everything the single-page UI needs, in one deterministic blob."""
    at = now or util.now_utc()
    session = pick_session(conn, session_id, at)
    findings = anomalies.detect_all(conn)
    scoped = [f for f in findings if session and f["session_id"] == session["id"]]

    blob = {
        "view": view,
        "generated_at": util.iso(at),
        "server_ms": int(util.epoch(at) * 1000),
        "sessions": list_sessions(conn),
        "session": dict(session) if session else None,
        "code": code_info(session, at) if session else None,
        "roster": roster(conn, session["id"] if session else None, at),
        "anomalies": findings,
        "session_anomalies": scoped,
        "anomaly_counts": anomalies.counts(findings),
        "summary": summary_rows(conn, session["id"] if session else None),
        "recent": recent_entries(conn, 8, session["id"] if session else None),
        "rejected": _db.many(conn, """SELECT * FROM rejected_attempts
                                      ORDER BY id DESC LIMIT 5"""),
        "chain": chain.verify(conn),
        "chain_tail": chain.tail(conn, 10),
        "totals": {
            "participants": _db.one(conn, "SELECT COUNT(*) AS n FROM participants")["n"],
            "sessions": _db.one(conn, "SELECT COUNT(*) AS n FROM sessions")["n"],
            "entries": _db.one(conn, "SELECT COUNT(*) AS n FROM attendance")["n"],
            "anomalies": len(findings),
        },
    }
    if session:
        blob["session"].pop("secret", None)
    return blob
