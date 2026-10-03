"""Shared test scaffolding: throwaway sqlite files and small builders."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest

from rollcall import codes, db as _db, service, util


class DbTestCase(unittest.TestCase):
    """A test case with a fresh sqlite file in a temp directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rollcall-test-")
        self.db_path = os.path.join(self.tmp, "test.sqlite3")
        self.conn = _db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()

    # ---- builders -------------------------------------------------------
    def open_session(self, label="Test Room", location="Room A", minutes=60, step_seconds=30,
                     opens_at=None, now=None, lat=None, lon=None):
        return service.create_session(self.conn, label, location, minutes=minutes,
                                      step_seconds=step_seconds, opens_at=opens_at,
                                      now=now or BASE, lat=lat, lon=lon)

    def code_at(self, session, when, back_steps=0):
        step = codes.step_for(util.epoch(when), session["step_seconds"]) - back_steps
        return codes.derive_code(session["secret"], session["id"], step)

    def check_in(self, session, ref, name, when, back_steps=0):
        return service.check_in(self.conn, session["id"], ref, name,
                                self.code_at(session, when, back_steps), now=when)


#: Fixed clock so every test is deterministic.
BASE = dt.datetime(2026, 10, 20, 9, 0, 0, tzinfo=util.UTC)


def at(minutes=0, seconds=0, hours=0, base=BASE):
    """A fixed UTC moment, measured from the test clock."""
    return util.plus(base, minutes=minutes, seconds=seconds, hours=hours)
