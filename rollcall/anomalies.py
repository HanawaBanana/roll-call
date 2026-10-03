"""Deterministic anomaly rules over the attendance log.

Four rules, each pure and individually unit tested.  Every flag carries a
human-readable *reason* and the exact attendance rows (*evidence*) it was derived
from, so a facilitator can check the claim instead of trusting it.

    duplicate_check_in          one participant, two accepted check-ins, same session
    code_reuse_after_rotation   a code from an earlier rotation step was accepted
    outside_session_window      check-in before opens_at or after closes_at
    impossible_travel           same person in two sessions too close in time

The rules are deterministic: same log in, same flags out, in the same order.
"""

from __future__ import annotations

import math

from . import db as _db
from . import util

MIN_TRAVEL_MINUTES = 30      # different rooms: a gap shorter than this is impossible
MAX_TRAVEL_KMH = 120.0       # same distance model when both sessions carry coordinates
EARTH_KM = 6371.0088

RULE_DUPLICATE = "duplicate_check_in"
RULE_CODE_REUSE = "code_reuse_after_rotation"
RULE_OUTSIDE_WINDOW = "outside_session_window"
RULE_TRAVEL = "impossible_travel"

SEVERITY = {
    RULE_DUPLICATE: "high",
    RULE_CODE_REUSE: "warn",
    RULE_OUTSIDE_WINDOW: "high",
    RULE_TRAVEL: "high",
}

RULE_DESCRIPTION = {
    RULE_DUPLICATE: "The same participant has more than one accepted check-in in one session.",
    RULE_CODE_REUSE: "A code that had already rotated was accepted (grace window) or the same code "
                     "string was accepted in two different sessions.",
    RULE_OUTSIDE_WINDOW: "The check-in timestamp falls before the session opened or after it closed.",
    RULE_TRAVEL: "The same participant checked into two different-location sessions closer in time "
                 "than it is physically possible to travel between them.",
}

#: Rules in the order they are reported.
RULE_ORDER = (RULE_DUPLICATE, RULE_OUTSIDE_WINDOW, RULE_CODE_REUSE, RULE_TRAVEL)

EVIDENCE_FIELDS = ("id", "seq", "session_id", "session_label", "session_location",
                   "participant_id", "participant_ref", "participant_name", "kind",
                   "checked_in_at", "code_used", "code_step", "session_step", "entry_hash")

SELECT_ROWS = """
SELECT a.id, a.seq, a.session_id, a.participant_id, a.kind, a.code_used, a.code_step,
       a.session_step, a.checked_in_at, a.recorded_at, a.source, a.note,
       a.prev_hash, a.entry_hash,
       p.ref AS participant_ref, p.name AS participant_name,
       s.label AS session_label, s.location AS session_location,
       s.opens_at AS session_opens_at, s.closes_at AS session_closes_at,
       s.lat AS session_lat, s.lon AS session_lon
FROM attendance a
JOIN participants p ON p.id = a.participant_id
JOIN sessions     s ON s.id = a.session_id
"""


def load_rows(conn, session_id=None) -> list:
    """Attendance rows joined with participant and session metadata, chain order."""
    sql = SELECT_ROWS
    args = ()
    if session_id is not None:
        sql += " WHERE a.session_id = ?"
        args = (int(session_id),)
    return _db.many(conn, sql + " ORDER BY a.seq ASC", args)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_KM * math.asin(math.sqrt(a))


def _evidence(rows) -> list:
    out = []
    for r in rows:
        out.append({k: r.get(k) for k in EVIDENCE_FIELDS})
    return out


def _finding(rule, reason, rows, participant=None, session_id=None, extra=None) -> dict:
    first = rows[0] if rows else {}
    finding = {
        "rule": rule,
        "severity": SEVERITY[rule],
        "rule_description": RULE_DESCRIPTION[rule],
        "reason": reason,
        "evidence_ids": [r["id"] for r in rows],
        "evidence": _evidence(rows),
        "participant_id": (participant or {}).get("id", first.get("participant_id")),
        "participant_ref": (participant or {}).get("ref", first.get("participant_ref")),
        "participant_name": (participant or {}).get("name", first.get("participant_name")),
        "session_id": session_id if session_id is not None else first.get("session_id"),
    }
    if extra:
        finding.update(extra)
    return finding


def detect_duplicates(rows) -> list:
    """Rule 1: same participant, two accepted check-ins in the same session."""
    seen = {}
    for r in rows:
        seen.setdefault((r["session_id"], r["participant_id"]), []).append(r)
    out = []
    for (session_id, _pid), group in seen.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r["seq"])
        first = ordered[0]
        out.append(_finding(
            RULE_DUPLICATE,
            "%s (%s) has %d accepted check-ins in session %d (%s): rows %s. "
            "First: %s, latest: %s." % (
                first["participant_name"], first["participant_ref"], len(ordered), session_id,
                first["session_label"], ", ".join("#%d" % r["id"] for r in ordered),
                first["checked_in_at"], ordered[-1]["checked_in_at"]),
            ordered, session_id=session_id))
    return out


def detect_outside_window(rows) -> list:
    """Rule 2: check-in timestamp outside [opens_at, closes_at]."""
    out = []
    for r in rows:
        at = util.parse_iso(r["checked_in_at"])
        opened = util.parse_iso(r["session_opens_at"])
        closed = util.parse_iso(r["session_closes_at"])
        if opened <= at <= closed:
            continue
        if at < opened:
            delta = (opened - at).total_seconds() / 60.0
            reason = ("check-in at %s is %.1f min BEFORE session %d (%s) opened at %s"
                      % (r["checked_in_at"], delta, r["session_id"], r["session_label"],
                         r["session_opens_at"]))
        else:
            delta = (at - closed).total_seconds() / 60.0
            reason = ("check-in at %s is %.1f min AFTER session %d (%s) closed at %s"
                      % (r["checked_in_at"], delta, r["session_id"], r["session_label"],
                         r["session_closes_at"]))
        out.append(_finding(RULE_OUTSIDE_WINDOW, reason, [r],
                            extra={"minutes_outside": round(delta, 1)}))
    return out


def detect_code_reuse(rows) -> list:
    """Rule 3: a code that had already rotated was accepted, or a code crossed sessions."""
    out = []
    for r in rows:
        if int(r["code_step"]) < int(r["session_step"]):
            back = int(r["session_step"]) - int(r["code_step"])
            out.append(_finding(
                RULE_CODE_REUSE,
                "%s (%s) typed code %s from rotation step %d during step %d of session %d "
                "(%s) - the code was already %d rotation(s) old and was accepted inside the "
                "1-step grace window." % (
                    r["participant_name"], r["participant_ref"], r["code_used"],
                    r["code_step"], r["session_step"], r["session_id"], r["session_label"], back),
                [r], extra={"steps_stale": back, "remote_reuse": False}))

    by_code = {}
    for r in rows:
        if r["code_used"]:
            by_code.setdefault(r["code_used"], []).append(r)
    for code, group in by_code.items():
        sessions = sorted({r["session_id"] for r in group})
        if len(sessions) < 2:
            continue
        ordered = sorted(group, key=lambda r: r["seq"])
        out.append(_finding(
            RULE_CODE_REUSE,
            "code %s was accepted in %d different sessions (%s) - a code only exists for one "
            "session secret, so it was copied from a screenshot or someone guessed twice."
            % (code, len(sessions), ", ".join("#%d" % s for s in sessions)),
            ordered, extra={"remote_reuse": True, "sessions": sessions}))
    return out


def detect_impossible_travel(rows) -> list:
    """Rule 4: one participant in two different-location sessions, too close in time."""
    by_participant = {}
    for r in rows:
        by_participant.setdefault(r["participant_id"], []).append(r)

    out = []
    for _pid, group in by_participant.items():
        ordered = sorted(group, key=lambda r: (r["checked_in_at"], r["seq"]))
        for left, right in zip(ordered, ordered[1:]):
            if left["session_id"] == right["session_id"]:
                continue
            if left["session_location"] == right["session_location"]:
                continue  # same room label: nothing impossible about it
            gap_min = ((util.parse_iso(right["checked_in_at"]) -
                        util.parse_iso(left["checked_in_at"])).total_seconds() / 60.0)
            coords = (left["session_lat"], left["session_lon"], right["session_lat"], right["session_lon"])
            if all(c is not None for c in coords):
                distance = haversine_km(*coords)
                speed = float("inf") if gap_min <= 0 else distance / (gap_min / 60.0)
                if speed <= MAX_TRAVEL_KMH:
                    continue
                detail = ("%.1f km between %s and %s in %.1f min = %.0f km/h (limit %.0f km/h)"
                          % (distance, left["session_location"], right["session_location"],
                             gap_min, speed, MAX_TRAVEL_KMH))
                extra = {"distance_km": round(distance, 2), "gap_minutes": round(gap_min, 1),
                         "implied_speed_kmh": round(speed, 1) if speed != float("inf") else None}
            else:
                if gap_min >= MIN_TRAVEL_MINUTES:
                    continue
                detail = ("no coordinates on file; %s and %s are different locations but only "
                          "%.1f min apart (limit %d min)"
                          % (left["session_location"], right["session_location"], gap_min,
                             MIN_TRAVEL_MINUTES))
                extra = {"distance_km": None, "gap_minutes": round(gap_min, 1),
                         "implied_speed_kmh": None}
            out.append(_finding(
                RULE_TRAVEL,
                "%s (%s) checked into session %d (%s) at %s and session %d (%s) at %s: %s."
                % (left["participant_name"], left["participant_ref"],
                   left["session_id"], left["session_label"], left["checked_in_at"],
                   right["session_id"], right["session_label"], right["checked_in_at"], detail),
                [left, right], extra=extra))
    return out


DETECTORS = {
    RULE_DUPLICATE: detect_duplicates,
    RULE_OUTSIDE_WINDOW: detect_outside_window,
    RULE_CODE_REUSE: detect_code_reuse,
    RULE_TRAVEL: detect_impossible_travel,
}


def detect_all(conn, session_id=None) -> list:
    """Run every rule over the log and return findings in a stable order."""
    rows = load_rows(conn, session_id)
    findings = []
    for rule in RULE_ORDER:
        findings.extend(DETECTORS[rule](rows))
    findings.sort(key=lambda f: (RULE_ORDER.index(f["rule"]),
                                 f["evidence_ids"][0] if f["evidence_ids"] else 0))
    return findings


def counts(findings) -> dict:
    """``{rule: n}`` plus ``{"total": n}``."""
    out = {"total": len(findings)}
    for rule in RULE_ORDER:
        out[rule] = sum(1 for f in findings if f["rule"] == rule)
    return out
