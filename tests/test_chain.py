"""The hash chain: every row sealed, every surgical edit detectable."""

from __future__ import annotations

import unittest

from rollcall import chain, util

from tests.support import at, DbTestCase


class ChainTests(DbTestCase):
    def build(self, rows=5):
        session = self.open_session()
        for i in range(rows):
            self.check_in(session, "P-%03d" % (i + 1), "Person %d" % (i + 1), at(minutes=i))
        return session

    def test_appended_rows_link_and_verify(self):
        self.build(4)
        result = chain.verify(self.conn)
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 4)
        self.assertEqual(result["total_rows"], 4)
        first = self.conn.execute("SELECT * FROM attendance ORDER BY seq LIMIT 1").fetchone()
        self.assertEqual(first["prev_hash"], chain.GENESIS)
        second = self.conn.execute("SELECT * FROM attendance ORDER BY seq LIMIT 1 OFFSET 1").fetchone()
        self.assertEqual(second["prev_hash"], first["entry_hash"])

    def test_empty_log_verifies_against_genesis(self):
        result = chain.verify(self.conn)
        self.assertTrue(result["ok"])
        self.assertEqual(result["head"], chain.GENESIS)
        self.assertEqual(result["checked"], 0)

    def test_editing_a_row_is_detected_with_the_first_broken_link(self):
        self.build(5)
        target = self.conn.execute("SELECT * FROM attendance ORDER BY seq LIMIT 1 OFFSET 2").fetchone()
        with chain.tx(self.conn):
            self.conn.execute("UPDATE attendance SET checked_in_at = ? WHERE id = ?",
                              ("2026-10-20T08:00:00Z", target["id"]))
        result = chain.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["first_broken"]["attendance_id"], target["id"])
        self.assertEqual(result["first_broken"]["reason"], chain.REASON_PAYLOAD)
        self.assertIn("payload", result["first_broken"]["detail"])

    def test_re_attributing_a_row_to_another_participant_is_detected(self):
        """The classic register fraud: pretend your friend was there (as you)."""
        self.build(3)
        rows = self.conn.execute("SELECT id, participant_id FROM attendance ORDER BY seq").fetchall()
        target, other = rows[0], rows[1]
        with chain.tx(self.conn):
            self.conn.execute("UPDATE attendance SET participant_id = ? WHERE id = ?",
                              (other["participant_id"], target["id"]))
        broken = chain.verify(self.conn)["first_broken"]
        self.assertEqual(broken["attendance_id"], target["id"])
        self.assertEqual(broken["reason"], chain.REASON_PAYLOAD)

    def test_foreign_keys_block_an_unknown_participant(self):
        self.build(2)
        with self.assertRaises(Exception):
            with chain.tx(self.conn):
                self.conn.execute("UPDATE attendance SET participant_id = 999 WHERE seq = 1")

    def test_deleting_the_last_row_is_detected_via_the_head_pointer(self):
        self.build(4)
        last = self.conn.execute("SELECT id FROM attendance ORDER BY seq DESC LIMIT 1").fetchone()
        with chain.tx(self.conn):
            self.conn.execute("DELETE FROM attendance WHERE id = ?", (last["id"],))
        result = chain.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_broken"]["reason"], chain.REASON_HEAD)
        self.assertIsNone(result["first_broken"]["attendance_id"])

    def test_deleting_a_middle_row_is_detected_as_a_sequence_gap(self):
        self.build(4)
        middle = self.conn.execute("SELECT id, seq FROM attendance ORDER BY seq LIMIT 1 OFFSET 1").fetchone()
        with chain.tx(self.conn):
            self.conn.execute("DELETE FROM attendance WHERE id = ?", (middle["id"],))
        broken = chain.verify(self.conn)["first_broken"]
        self.assertEqual(broken["reason"], chain.REASON_SEQ_GAP)
        self.assertEqual(broken["seq"], middle["seq"] + 1)

    def test_a_hand_written_row_breaks_the_link(self):
        self.build(2)
        pid = self.conn.execute("SELECT id FROM participants ORDER BY id LIMIT 1").fetchone()["id"]
        with chain.tx(self.conn):
            self.conn.execute(
                """INSERT INTO attendance
                   (seq, session_id, participant_id, kind, code_used, code_step, session_step,
                    checked_in_at, recorded_at, source, note, prev_hash, entry_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (99, 1, pid, "checkin", "AAAAAA", 0, 0, util.iso(at(minutes=30)), util.iso(at()),
                 "forged", "", chain.GENESIS, "f" * 64))
        result = chain.verify(self.conn)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first_broken"]["reason"], chain.REASON_SEQ_GAP)

    def test_payload_covers_every_forensic_field(self):
        self.assertEqual(set(chain.PAYLOAD_FIELDS),
                         {"seq", "session_id", "participant_id", "kind", "code_used", "code_step",
                          "session_step", "checked_in_at", "recorded_at", "source"})

    def test_head_pointer_tracks_the_last_row(self):
        self.build(3)
        state = chain.head(self.conn)
        last = self.conn.execute("SELECT * FROM attendance ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(state["last_seq"], last["seq"])
        self.assertEqual(state["head_hash"], last["entry_hash"])

    def test_tamper_helper_reports_before_and_after(self):
        self.build(3)
        out = chain.tamper(self.conn, 2, "checked_in_at", "2020-01-01T00:00:00Z")
        self.assertEqual(out["after"], "2020-01-01T00:00:00Z")
        self.assertNotEqual(out["before"], out["after"])
        self.assertFalse(chain.verify(self.conn)["ok"])
        self.assertEqual(chain.verify(self.conn)["first_broken"]["attendance_id"], 2)


if __name__ == "__main__":
    unittest.main()
