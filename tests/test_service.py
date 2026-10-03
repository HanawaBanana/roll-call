"""Service layer: sessions, check-in rules, roster, summary, exports, demo seed."""

from __future__ import annotations

import json
import unittest

from rollcall import anomalies, chain, codes, demo, exports, service, util

from tests.support import at, DbTestCase, BASE


class SessionTests(DbTestCase):
    def test_create_session_sets_the_window_and_hides_the_secret(self):
        session = self.open_session(label="Week 6", location="Room 204", minutes=90)
        self.assertEqual(session["opens_at"], util.iso(BASE))
        self.assertEqual(session["closes_at"], util.iso(at(minutes=90)))
        self.assertEqual(len(session["secret"]), 64)
        listed = service.list_sessions(self.conn)
        self.assertEqual(len(listed), 1)
        self.assertNotIn("secret", listed[0])
        self.assertEqual(listed[0]["entries"], 0)

    def test_create_session_rejects_bad_input(self):
        with self.assertRaises(service.ServiceError):
            service.create_session(self.conn, "   ")
        with self.assertRaises(service.ServiceError):
            service.create_session(self.conn, "ok", minutes=0)
        with self.assertRaises(service.ServiceError):
            service.create_session(self.conn, "ok", step_seconds=1)

    def test_backdated_session_is_allowed(self):
        session = service.create_session(self.conn, "Substitute", opens_at=util.iso(at(minutes=-30)),
                                         minutes=60, now=BASE)
        self.assertEqual(session["opens_at"], util.iso(at(minutes=-30)))

    def test_code_info_countdown_and_current_code(self):
        session = self.open_session(step_seconds=20)
        info = service.code_info(session, at(minutes=1, seconds=5))
        self.assertEqual(info["step_seconds"], 20)
        self.assertEqual(info["seconds_left"], 15)
        self.assertEqual(info["current"],
                         codes.derive_code(session["secret"], session["id"], info["step"]))
        self.assertNotEqual(info["current"], info["previous"])
        self.assertEqual(info["expires_at_ms"] - info["server_ms"], 15000)

    def test_close_session_stops_check_ins(self):
        session = self.open_session()
        service.close_session(self.conn, session["id"], now=at(minutes=5))
        result = service.check_in(self.conn, session["id"], "P-001", "Amina",
                                  self.code_at(session, at(minutes=6)), now=at(minutes=6))
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "session_closed")
        self.assertEqual(service.state(self.conn)["totals"]["entries"], 0)


class CheckInTests(DbTestCase):
    def test_accepted_check_in_seals_a_chain_row(self):
        session = self.open_session()
        result = self.check_in(session, "P-001", "Amina Belkacem", at(minutes=3))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["seq"], 1)
        self.assertEqual(result["kind"], "checkin")
        self.assertFalse(result["stale_code"])
        self.assertEqual(len(result["entry_hash"]), 64)
        row = self.conn.execute("SELECT * FROM attendance").fetchone()
        self.assertEqual(row["participant_id"], result["participant"]["id"])
        self.assertEqual(row["checked_in_at"], util.iso(at(minutes=3)))
        self.assertTrue(chain.verify(self.conn)["ok"])

    def test_expired_code_is_refused_and_kept_as_evidence(self):
        session = self.open_session(step_seconds=30)
        result = self.check_in(session, "P-002", "Bruno", at(minutes=10), back_steps=4)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "expired")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 0)
        rejected = self.conn.execute("SELECT * FROM rejected_attempts").fetchone()
        self.assertEqual(rejected["reason"], "expired")
        self.assertEqual(rejected["participant_ref"], "P-002")

    def test_missing_code_is_refused(self):
        session = self.open_session()
        result = service.check_in(self.conn, session["id"], "P-003", "Chen", None, now=at())
        self.assertEqual(result["reason"], "missing_code")

    def test_unknown_reference_needs_a_name_once(self):
        session = self.open_session()
        result = service.check_in(self.conn, session["id"], "P-004", None,
                                  self.code_at(session, at()), now=at())
        self.assertEqual(result["reason"], "invalid_participant")
        ok = self.check_in(session, "P-004", "Daria", at(minutes=1))
        self.assertEqual(ok["status"], "accepted")
        again = self.check_in(session, "P-004", None, at(minutes=2))
        self.assertEqual(again["status"], "duplicate")

    def test_second_check_in_is_a_duplicate_not_an_error(self):
        session = self.open_session()
        self.check_in(session, "P-005", "Elif", at(minutes=1))
        result = self.check_in(session, "P-005", "Elif", at(minutes=2))
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["kind"], "duplicate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 2)
        flags = anomalies.detect_all(self.conn)
        self.assertEqual([f["rule"] for f in flags], [anomalies.RULE_DUPLICATE])

    def test_code_from_the_grace_window_is_accepted_and_flagged_stale(self):
        session = self.open_session(step_seconds=30)
        result = self.check_in(session, "P-006", "Farid", at(minutes=5), back_steps=1)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["stale_code"])
        findings = anomalies.detect_all(self.conn)
        self.assertEqual([f["rule"] for f in findings], [anomalies.RULE_CODE_REUSE])

    def test_check_in_after_the_window_closed_is_recorded_and_flagged(self):
        session = self.open_session(minutes=30)
        result = self.check_in(session, "P-007", "Greta", at(minutes=40))
        self.assertEqual(result["status"], "accepted")
        findings = anomalies.detect_all(self.conn)
        self.assertEqual([f["rule"] for f in findings], [anomalies.RULE_OUTSIDE_WINDOW])


class ViewTests(DbTestCase):
    def setUp(self):
        super(ViewTests, self).setUp()
        self.session = self.open_session(location="Room 204", minutes=60)
        self.check_in(self.session, "P-101", "Amina", at(minutes=1))
        self.check_in(self.session, "P-102", "Bruno", at(minutes=2))
        self.check_in(self.session, "P-101", "Amina", at(minutes=3))

    def test_roster_marks_presence_and_flags(self):
        roster = service.roster(self.conn, self.session["id"], at(minutes=10))
        by_ref = {r["ref"]: r for r in roster}
        self.assertTrue(by_ref["P-101"]["present"])
        self.assertEqual(by_ref["P-101"]["entries"], 2)
        self.assertIn(anomalies.RULE_DUPLICATE, by_ref["P-101"]["flags"])
        self.assertEqual(by_ref["P-102"]["entries"], 1)
        self.assertEqual(by_ref["P-101"]["first_checkin"], util.iso(at(minutes=1)))

    def test_summary_counts_sessions_and_entries(self):
        second = self.open_session(label="Second", location="Room 118", minutes=60)
        self.check_in(second, "P-101", "Amina", at(minutes=30))
        rows = service.summary_rows(self.conn, None)
        amina = [r for r in rows if r["ref"] == "P-101"][0]
        self.assertEqual(amina["sessions_attended"], 2)
        self.assertEqual(amina["sessions_total"], 2)
        self.assertEqual(amina["entries"], 3)
        self.assertEqual(amina["duplicates"], 1)
        scoped = service.summary_rows(self.conn, self.session["id"])
        self.assertEqual([r for r in scoped if r["ref"] == "P-101"][0]["sessions_total"], 1)

    def test_state_blob_exposes_everything_the_ui_needs(self):
        blob = service.state(self.conn, view="roster")
        for key in ("view", "sessions", "session", "code", "roster", "anomalies", "anomaly_counts",
                    "summary", "recent", "chain", "chain_tail", "totals", "server_ms"):
            self.assertIn(key, blob)
        self.assertEqual(blob["view"], "roster")
        self.assertNotIn("secret", blob["session"])
        self.assertTrue(blob["chain"]["ok"])
        self.assertEqual(blob["totals"]["participants"], 2)
        json.dumps(blob)  # must be serialisable for the API

    def test_recent_entries_are_newest_first(self):
        recent = service.recent_entries(self.conn, 3, self.session["id"])
        self.assertEqual([r["seq"] for r in recent], [3, 2, 1])
        self.assertEqual(recent[0]["participant_ref"], "P-101")


class ExportTests(DbTestCase):
    def test_csv_exports_have_headers_and_one_row_per_entry(self):
        session = self.open_session(minutes=30)
        for i in range(3):
            self.check_in(session, "P-%03d" % (i + 1), "Person %d" % (i + 1), at(minutes=i))
        self.check_in(session, "P-004", "Late", at(minutes=45))

        text = exports.attendance_csv(self.conn)
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("attendance_id,seq,session_id,session_label"))
        self.assertIn("entry_hash", lines[0])
        self.assertTrue(all(len(line.split(",")) == len(exports.ATTENDANCE_COLUMNS) for line in lines))

        summary = exports.summary_csv(self.conn).strip().split("\n")
        self.assertEqual(summary[0].split(",")[:3], ["participant_ref", "participant_name", "sessions_attended"])
        self.assertEqual(len(summary), 5)

        flagged = exports.anomalies_csv(self.conn).strip().split("\n")
        self.assertEqual(len(flagged), 2)
        self.assertIn("outside_session_window", flagged[1])

    def test_json_bundle_is_parseable_and_chain_backed(self):
        session = self.open_session()
        self.check_in(session, "P-001", "Amina", at(minutes=1))
        payload = json.loads(exports.json_bundle(self.conn))
        self.assertEqual(payload["schema"], "rollcall/v1")
        self.assertEqual(payload["totals"]["entries"], 1)
        self.assertEqual(payload["attendance"][0]["participant_ref"], "P-001")
        self.assertTrue(payload["chain"]["ok"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_scoped_export_only_contains_that_session(self):
        one = self.open_session(label="One")
        two = self.open_session(label="Two", opens_at=util.iso(at(minutes=1)))
        self.check_in(one, "P-001", "Amina", at(minutes=2))
        self.check_in(two, "P-002", "Bruno", at(minutes=3))
        scoped = exports.attendance_csv(self.conn, one["id"]).strip().split("\n")
        self.assertEqual(len(scoped), 2)
        self.assertIn("One", scoped[1])


class DemoTests(DbTestCase):
    def test_demo_seed_is_reproducible_and_shows_all_four_rules(self):
        report = demo.seed(self.conn, now=BASE)
        self.assertEqual(len(report["sessions"]), 2)
        self.assertEqual(report["accepted"], 12)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["rejected"], 1)
        self.assertEqual(report["rejected_reason"], "expired")

        findings = anomalies.detect_all(self.conn)
        rules = sorted({f["rule"] for f in findings})
        self.assertEqual(rules, sorted([anomalies.RULE_DUPLICATE, anomalies.RULE_OUTSIDE_WINDOW,
                                        anomalies.RULE_CODE_REUSE, anomalies.RULE_TRAVEL]))
        self.assertEqual(anomalies.counts(findings)["total"], 4)
        self.assertTrue(chain.verify(self.conn)["ok"])
        self.assertEqual(chain.verify(self.conn)["checked"], 13)

    def test_demo_seed_is_deterministic_given_the_same_clock(self):
        demo.seed(self.conn, now=BASE)
        first = json.dumps(anomalies.detect_all(self.conn), sort_keys=True)
        self.assertEqual(first, json.dumps(anomalies.detect_all(self.conn), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
