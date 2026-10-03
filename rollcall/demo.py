"""The deterministic demo scenario: two sessions with a real, findable history.

``python3 cli.py seed --demo`` builds this from *now* so the seeded rows look like
a club that met two hours ago.  Every round trip goes through
:mod:`rollcall.service`, so the demo data is produced by exactly the same code
path as a real check-in, including the hash chain.
"""

from __future__ import annotations

import datetime as _dt

from . import codes, service, util

SESSION_ONE = "Robotics Club - Build Night"
ROOM_ONE = "Room 204 (Main Lab)"
SESSION_TWO = "Robotics Club - Sign-off Desk"
ROOM_TWO = "Room 118 (Front Office)"

ROSTER = [
    ("P-101", "Amina Belkacem"),
    ("P-102", "Bruno Tavares"),
    ("P-103", "Chen Wei"),
    ("P-104", "Daria Novak"),
    ("P-105", "Elif Kaya"),
    ("P-106", "Farid Haddad"),
    ("P-107", "Greta Lindqvist"),
    ("P-108", "Hugo Moreau"),
    ("P-109", "Iris Van Dijk"),
    ("P-110", "Jonas Petersen"),
    ("P-111", "Keiko Tanaka"),
    ("P-112", "Liam O'Doherty"),
]

STEP_SECONDS = 30


def _code_at(session, when, back_steps=0):
    """The code a person read off the facilitator screen at ``when``."""
    step = codes.step_for(util.epoch(when), session["step_seconds"]) - back_steps
    return codes.derive_code(session["secret"], session["id"], step)


def seed(conn, now=None) -> dict:
    """Build the demo history.  Returns a small report of what was created."""
    at = now or util.now_utc()
    day = util.minus(at, hours=2)

    one = service.create_session(conn, SESSION_ONE, ROOM_ONE, minutes=60,
                                 step_seconds=STEP_SECONDS, opens_at=util.iso(day), now=at)
    two = service.create_session(conn, SESSION_TWO, ROOM_TWO, minutes=60,
                                 step_seconds=STEP_SECONDS,
                                 opens_at=util.iso(util.plus(day, minutes=4)), now=at)
    report = {"sessions": [one["id"], two["id"]], "accepted": 0, "rejected": 0, "duplicates": 0}

    # --- session one: the room fills up -------------------------------------
    on_time = [
        ("P-101", 1), ("P-102", 2), ("P-103", 3), ("P-104", 4), ("P-105", 6),
        ("P-106", 7), ("P-108", 9), ("P-109", 11), ("P-111", 13),
    ]
    for ref, minutes in on_time:
        name = dict(ROSTER)[ref]
        when = util.plus(day, minutes=minutes)
        out = service.check_in(conn, one["id"], ref, name, _code_at(one, when), now=when)
        report["accepted" if out["status"] == "accepted" else "rejected"] += 1

    # --- the same person checks in twice (phone reloaded the form) ----------
    when = util.plus(day, minutes=18)
    out = service.check_in(conn, one["id"], "P-103", "Chen Wei", _code_at(one, when), now=when)
    report["duplicates" if out["status"] == "duplicate" else "rejected"] += 1

    # --- someone wrote the code down and typed it after it rotated ----------
    when = util.plus(day, minutes=22)
    out = service.check_in(conn, one["id"], "P-110", "Jonas Petersen",
                           _code_at(one, when, back_steps=1), now=when)
    report["accepted" if out["status"] == "accepted" else "rejected"] += 1

    # --- a check-in that lands 12 minutes after the session closed ----------
    when = util.plus(day, minutes=72)
    out = service.check_in(conn, one["id"], "P-107", "Greta Lindqvist", _code_at(one, when), now=when)
    report["accepted" if out["status"] == "accepted" else "rejected"] += 1

    # --- the same person is in a second room four minutes later -------------
    when = util.plus(day, minutes=5)
    out = service.check_in(conn, two["id"], "P-104", "Daria Novak", _code_at(two, when), now=when)
    report["accepted" if out["status"] == "accepted" else "rejected"] += 1

    # --- and one attempt with an expired code, kept as evidence -------------
    when = util.plus(day, minutes=40)
    out = service.check_in(conn, one["id"], "P-112", "Liam O'Doherty",
                           _code_at(one, when, back_steps=6), now=when)
    report["rejected" if out["status"] == "rejected" else "accepted"] += 1
    report["rejected_reason"] = out.get("reason")

    one = service.get_session(conn, one["id"])
    two = service.get_session(conn, two["id"])
    report["session_one_id"] = one["id"]
    report["session_two_id"] = two["id"]
    report["day"] = util.iso(day)
    return report


def demo_participants(prefix="P-3", count=12):
    """A fresh batch of participants for a live demo session."""
    return [("%s%02d" % (prefix, i + 1), name) for i, (_ref, name) in enumerate(ROSTER[:count])]


def _unused():  # pragma: no cover - keeps the import used when running lints
    return _dt.datetime
