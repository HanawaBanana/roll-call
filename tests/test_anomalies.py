"""Every anomaly rule, one test per branch, with its evidence."""

from __future__ import annotations

import json
import unittest

from rollcall import anomalies, chain, service, util

from tests.support import at, DbTestCase, BASE


class AnomalyTests(DbTestCase):
    def rules(self, findings):
        return [f["rule"] for f in findings]

    def test_clean_session_has_no_flags(self):
        session = self.open_session()
        for i in range(4):
            self.check_in(session, "P-%03d" % (i + 1), "Person %d" % (i + 1), at(minutes=i))
        findings = anomalies.detect_all(self.conn)
        self.assertEqual(findings, [])
        self.assertEqual(anomalies.counts(findings)["total"], 0)

    def test_duplicate_check_in_carries_both_rows_as_evidence(self):
        session = self.open_session()
        self.check_in(session, "P-001", "Amina", at(minutes=1))
        self.check_in(session, "P-001", "Amina", at(minutes=2))
        findings = anomalies.detect_all(self.conn)
        self.assertEqual(self.rules(findings), [anomalies.RULE_DUPLICATE])
        flag = findings[0]
        self.assertEqual(flag["participant_ref"], "P-001")
        self.assertEqual(len(flag["evidence_ids"]), 2)
        self.assertEqual(flag["evidence_ids"], [r["id"] for r in flag["evidence"]])
        self.assertIn("2 accepted check-ins", flag["reason"])
        self.assertEqual(flag["severity"], "high")

    def test_outside_window_after_close_and_before_open(self):
        late = self.open_session(label="Late", minutes=30)
        self.check_in(late, "P-010", "Late Person", at(minutes=45))
        early = self.open_session(label="Early", minutes=60, opens_at=util.iso(at(minutes=30)))
        self.check_in(early, "P-011", "Early Person", at(minutes=10))
        findings = anomalies.detect_all(self.conn)
        self.assertEqual(sorted(self.rules(findings)),
                         [anomalies.RULE_OUTSIDE_WINDOW] * 2)
        details = " ".join(f["reason"] for f in findings)
        self.assertIn("AFTER session", details)
        self.assertIn("BEFORE session", details)
        self.assertEqual(sum(len(f["evidence"]) for f in findings), 2)

    def test_check_inside_the_window_is_not_flagged(self):
        session = self.open_session(minutes=60)
        self.check_in(session, "P-001", "Amina", at(minutes=59))
        self.assertEqual(anomalies.detect_all(self.conn), [])

    def test_code_reuse_after_rotation(self):
        session = self.open_session(step_seconds=30)
        self.check_in(session, "P-001", "Amina", at(minutes=1), back_steps=1)
        findings = anomalies.detect_all(self.conn)
        self.assertEqual(self.rules(findings), [anomalies.RULE_CODE_REUSE])
        flag = findings[0]
        self.assertTrue(flag["evidence"][0]["code_step"] < flag["evidence"][0]["session_step"])
        self.assertIn("grace window", flag["reason"])
        self.assertEqual(flag["severity"], "warn")

    def test_same_code_string_in_two_sessions_is_flagged(self):
        """Only a hand-crafted row can do this, which is exactly the point."""
        one = self.open_session(label="One")
        two = self.open_session(label="Two")
        person = service.ensure_participant(self.conn, "P-500", "Forged Person", at())
        for session, minute in ((one, 1), (two, 30)):
            chain.append(self.conn, session_id=session["id"], participant_id=person["id"],
                         kind="checkin", code_used="55555A", code_step=1, session_step=1,
                         checked_in_at=util.iso(at(minutes=minute)), recorded_at=util.iso(at()))
        findings = [f for f in anomalies.detect_all(self.conn) if f["rule"] == anomalies.RULE_CODE_REUSE]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["remote_reuse"])
        self.assertEqual(findings[0]["sessions"], [one["id"], two["id"]])

    def test_impossible_travel_between_two_rooms(self):
        one = self.open_session(label="Build night", location="Room 204", minutes=60)
        two = self.open_session(label="Sign-off", location="Room 118", minutes=60)
        self.check_in(one, "P-104", "Daria", at(minutes=4))
        self.check_in(two, "P-104", "Daria", at(minutes=5))
        findings = anomalies.detect_all(self.conn)
        travel = [f for f in findings if f["rule"] == anomalies.RULE_TRAVEL]
        self.assertEqual(len(travel), 1)
        self.assertEqual(len(travel[0]["evidence"]), 2)
        self.assertEqual(travel[0]["gap_minutes"], 1.0)
        self.assertIn("different locations", travel[0]["reason"])

    def test_same_room_between_sessions_is_not_impossible(self):
        one = self.open_session(label="Morning", location="Room 204", minutes=60)
        two = self.open_session(label="Afternoon", location="Room 204", minutes=60)
        self.check_in(one, "P-104", "Daria", at(minutes=4))
        self.check_in(two, "P-104", "Daria", at(minutes=5))
        self.assertEqual([f for f in anomalies.detect_all(self.conn)
                          if f["rule"] == anomalies.RULE_TRAVEL], [])

    def test_two_different_rooms_far_apart_in_time_are_not_flagged(self):
        one = self.open_session(label="Build night", location="Room 204", minutes=60)
        two = self.open_session(label="Sign-off", location="Room 118", minutes=600,
                                opens_at=util.iso(at(hours=8)))
        self.check_in(one, "P-104", "Daria", at(minutes=4))
        self.check_in(two, "P-104", "Daria", at(hours=8))
        self.assertEqual([f for f in anomalies.detect_all(self.conn)
                          if f["rule"] == anomalies.RULE_TRAVEL], [])

    def test_coordinates_make_it_a_speed_calculation(self):
        """Paris to London: 344 km. Impossible in 30 minutes, fine in 5 hours."""
        paris = self.open_session(label="Paris site", location="Paris", minutes=600,
                                  lat=48.8566, lon=2.3522)
        london = self.open_session(label="London site", location="London", minutes=600,
                                   lat=51.5074, lon=-0.1278)
        self.check_in(paris, "P-201", "Traveller", at(minutes=1))
        self.check_in(london, "P-201", "Traveller", at(minutes=31))
        travel = [f for f in anomalies.detect_all(self.conn) if f["rule"] == anomalies.RULE_TRAVEL]
        self.assertEqual(len(travel), 1)
        self.assertGreater(travel[0]["distance_km"], 300)
        self.assertGreater(travel[0]["implied_speed_kmh"], anomalies.MAX_TRAVEL_KMH)
        self.assertIn("km/h", travel[0]["reason"])

    def test_same_route_with_five_hours_is_not_flagged(self):
        paris = self.open_session(label="Paris site", location="Paris", minutes=600,
                                  lat=48.8566, lon=2.3522)
        london = self.open_session(label="London site", location="London", minutes=600,
                                   opens_at=util.iso(at(hours=6)), lat=51.5074, lon=-0.1278)
        self.check_in(paris, "P-201", "Traveller", at(minutes=1))
        self.check_in(london, "P-201", "Traveller", at(hours=6))
        self.assertEqual([f for f in anomalies.detect_all(self.conn)
                          if f["rule"] == anomalies.RULE_TRAVEL], [])

    def test_haversine_distance_is_sane(self):
        self.assertAlmostEqual(anomalies.haversine_km(48.8566, 2.3522, 48.8566, 2.3522), 0.0, places=6)
        self.assertAlmostEqual(anomalies.haversine_km(48.8566, 2.3522, 51.5074, -0.1278), 343, delta=6)

    def test_rules_are_deterministic_and_ordered(self):
        session = self.open_session(label="Mixed", location="Room A", minutes=30)
        self.check_in(session, "P-001", "Amina", at(minutes=1))
        self.check_in(session, "P-001", "Amina", at(minutes=2))
        self.check_in(session, "P-002", "Bruno", at(minutes=40))
        self.check_in(session, "P-003", "Chen", at(minutes=3), back_steps=1)
        first = anomalies.detect_all(self.conn)
        second = anomalies.detect_all(self.conn)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        order = [anomalies.RULE_ORDER.index(f["rule"]) for f in first]
        self.assertEqual(order, sorted(order))
        self.assertEqual(anomalies.counts(first)["total"], 3)

    def test_every_flag_carries_a_reason_and_evidence_rows(self):
        session = self.open_session(label="Mixed", location="Room A", minutes=30)
        self.check_in(session, "P-001", "Amina", at(minutes=1))
        self.check_in(session, "P-001", "Amina", at(minutes=2))
        self.check_in(session, "P-002", "Bruno", at(minutes=40))
        for flag in anomalies.detect_all(self.conn):
            self.assertTrue(flag["reason"])
            self.assertTrue(flag["evidence"])
            self.assertEqual(len(flag["evidence"]), len(flag["evidence_ids"]))
            for row in flag["evidence"]:
                self.assertIn(row["id"], flag["evidence_ids"])
                self.assertTrue(row["entry_hash"])


if __name__ == "__main__":
    unittest.main()
