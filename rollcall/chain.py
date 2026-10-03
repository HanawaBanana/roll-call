"""Tamper-evident attendance log: a hash chain over every check-in event.

Every attendance row is sealed as::

    entry_hash = sha256("rollcall/v1/entry" || prev_hash || canonical_json(payload))

where ``payload`` is the fixed field list in :data:`PAYLOAD_FIELDS` and
``prev_hash`` is the ``entry_hash`` of the previous row (``seq`` order).  The
genesis link is 64 zeros.

The head of the chain (last sequence number and last hash) is mirrored in the
``chain_state`` table so that *deleting* rows from the end of the log is
detectable too, not just editing them.

What this proves and what it does not is spelled out in README.md ("Threat
model"): without an append-only external anchor, an attacker who can rewrite the
whole database can recompute the chain.  For a class register the useful
property is that a *surgical* edit -- changing one attendance time, adding a
ghost row, deleting an inconvenient one -- stops verifying, and :func:`verify`
prints the first broken link with the row that caused it.
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3

from . import db, util

GENESIS = "0" * 64
HASH_DOMAIN = b"rollcall/v1/entry"

#: Fields that are hashed.  Anything outside this list is not protected.
PAYLOAD_FIELDS = (
    "seq",
    "session_id",
    "participant_id",
    "kind",
    "code_used",
    "code_step",
    "session_step",
    "checked_in_at",
    "recorded_at",
    "source",
)

REASON_PAYLOAD = "payload_mismatch"
REASON_LINK = "link_mismatch"
REASON_SEQ_GAP = "sequence_gap"
REASON_HEAD = "head_mismatch"


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Write transaction (``BEGIN IMMEDIATE``) so appends serialise."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def payload_of(row: dict) -> dict:
    """Extract the hashed payload from an attendance row."""
    return {k: row[k] for k in PAYLOAD_FIELDS}


def compute_hash(prev_hash: str, payload: dict) -> str:
    """Hash of one link: domain || prev_hash || canonical payload."""
    material = HASH_DOMAIN + prev_hash.encode("ascii") + util.canonical(payload).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def head(conn: sqlite3.Connection) -> dict:
    """Current chain head, initialising it on first use."""
    row = db.one(conn, "SELECT * FROM chain_state WHERE id = 1")
    if row is None:
        return {"last_seq": 0, "head_hash": GENESIS}
    return row


def append(conn: sqlite3.Connection, *, session_id: int, participant_id: int, kind: str,
           code_used: str, code_step: int, session_step: int, checked_in_at: str,
           recorded_at: str, source: str = "web", note: str = "") -> dict:
    """Seal one attendance event onto the end of the chain.  Returns the new row."""
    with tx(conn):
        state = db.one(conn, "SELECT * FROM chain_state WHERE id = 1")
        if state is None:
            prev_hash, seq = GENESIS, 1
        else:
            prev_hash, seq = state["head_hash"], int(state["last_seq"]) + 1

        row = {
            "seq": seq,
            "session_id": int(session_id),
            "participant_id": int(participant_id),
            "kind": kind,
            "code_used": code_used,
            "code_step": int(code_step),
            "session_step": int(session_step),
            "checked_in_at": checked_in_at,
            "recorded_at": recorded_at,
            "source": source,
        }
        entry_hash = compute_hash(prev_hash, payload_of(row))

        cur = conn.execute(
            """INSERT INTO attendance
               (seq, session_id, participant_id, kind, code_used, code_step, session_step,
                checked_in_at, recorded_at, source, note, prev_hash, entry_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (seq, row["session_id"], row["participant_id"], kind, code_used, row["code_step"],
             row["session_step"], checked_in_at, recorded_at, source, note, prev_hash, entry_hash),
        )
        attendance_id = cur.lastrowid
        conn.execute(
            """INSERT INTO chain_state (id, last_seq, head_hash, updated_at) VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET last_seq = excluded.last_seq,
                                             head_hash = excluded.head_hash,
                                             updated_at = excluded.updated_at""",
            (seq, entry_hash, recorded_at),
        )
    out = dict(row)
    out["id"] = attendance_id
    out["prev_hash"] = prev_hash
    out["entry_hash"] = entry_hash
    out["note"] = note
    return out


def verify(conn: sqlite3.Connection) -> dict:
    """Walk the chain and report the *first* broken link.

    Returns a dict with ``ok``, ``checked`` (rows that validated from the start),
    ``total_rows``, the recomputed ``head``, and ``first_broken`` -- either
    ``None`` or ``{attendance_id, seq, reason, detail, stored_hash, expected_hash}``.
    """
    state = db.one(conn, "SELECT * FROM chain_state WHERE id = 1")
    rows = db.many(conn, "SELECT * FROM attendance ORDER BY seq ASC")

    prev_hash = GENESIS
    expected_seq = 1
    checked = 0
    first_broken = None

    for row in rows:
        broken = None
        if int(row["seq"]) != expected_seq:
            _, _, missing = expected_seq, row["seq"], row["seq"] - expected_seq
            broken = (REASON_SEQ_GAP, "expected sequence %d but found %d (%d row(s) missing)"
                      % (expected_seq, row["seq"], missing), None)
        elif row["prev_hash"] != prev_hash:
            broken = (REASON_LINK, "row %d does not chain onto the previous row" % row["id"], prev_hash)
        else:
            recomputed = compute_hash(row["prev_hash"], payload_of(row))
            if recomputed != row["entry_hash"]:
                broken = (REASON_PAYLOAD,
                          "row %d payload no longer hashes to its sealed value "
                          "(fields changed or row forged)" % row["id"], recomputed)

        if broken is not None:
            if first_broken is None:
                first_broken = {
                    "attendance_id": row["id"],
                    "seq": row["seq"],
                    "reason": broken[0],
                    "detail": broken[1],
                    "stored_hash": row["entry_hash"],
                    "expected_hash": broken[2],
                }
        elif first_broken is None:
            checked += 1

        prev_hash = row["entry_hash"]
        expected_seq = int(row["seq"]) + 1

    stored_head = state["head_hash"] if state else GENESIS
    stored_last_seq = int(state["last_seq"]) if state else 0
    if first_broken is None:
        if stored_last_seq != expected_seq - 1:
            first_broken = {
                "attendance_id": None, "seq": stored_last_seq, "reason": REASON_HEAD,
                "detail": "chain head says %d row(s), the log holds %d"
                          % (stored_last_seq, expected_seq - 1),
                "stored_hash": stored_head, "expected_hash": prev_hash,
            }
        elif stored_head != prev_hash:
            first_broken = {
                "attendance_id": None, "seq": expected_seq - 1, "reason": REASON_HEAD,
                "detail": "chain head hash does not match the last row (%s)" % REASON_HEAD,
                "stored_hash": stored_head, "expected_hash": prev_hash,
            }

    return {
        "ok": first_broken is None,
        "checked": checked,
        "total_rows": len(rows),
        "head": prev_hash,
        "expected_seq": expected_seq - 1,
        "first_broken": first_broken,
    }


def tail(conn: sqlite3.Connection, limit: int = 12) -> list:
    """Most recent links, newest first, with a short hash form for display."""
    rows = db.many(conn, "SELECT * FROM attendance ORDER BY seq DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "seq": r["seq"], "session_id": r["session_id"],
            "participant_id": r["participant_id"], "kind": r["kind"],
            "checked_in_at": r["checked_in_at"], "code_used": r["code_used"],
            "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"],
            "prev_short": r["prev_hash"][:12], "hash_short": r["entry_hash"][:12],
        })
    return out


def tamper(conn: sqlite3.Connection, attendance_id: int, field: str = "checked_in_at",
           value: str = None) -> dict:
    """Deliberately corrupt one row (demo / red-team helper).  Returns before/after."""
    allowed = {"checked_in_at", "recorded_at", "code_used", "session_step", "code_step", "kind"}
    if field not in allowed:
        raise ValueError("cannot tamper field %r; allowed: %s" % (field, sorted(allowed)))
    before = db.one(conn, "SELECT * FROM attendance WHERE id = ?", (int(attendance_id),))
    if before is None:
        raise ValueError("no attendance row with id %s" % attendance_id)
    if value is None:
        value = util.iso(util.minus(util.parse_iso(before["checked_in_at"]), minutes=17))
    with tx(conn):
        conn.execute("UPDATE attendance SET %s = ? WHERE id = ?" % field, (value, int(attendance_id)))
    after = db.one(conn, "SELECT * FROM attendance WHERE id = ?", (int(attendance_id),))
    return {"field": field, "before": before[field], "after": after[field],
            "attendance_id": int(attendance_id), "row": after}
