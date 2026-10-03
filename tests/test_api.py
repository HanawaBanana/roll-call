"""End to end over real HTTP: the server, the JSON API and the export routes."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from rollcall import chain, web

from tests.support import DbTestCase


def request(url, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


class ApiTestCase(DbTestCase):
    def setUp(self):
        super(ApiTestCase, self).setUp()
        self.server = web.make_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super(ApiTestCase, self).tearDown()

    def get_json(self, path):
        status, _headers, body = request(self.base + path)
        return status, json.loads(body)

    def post_json(self, path, payload):
        status, _headers, body = request(self.base + path, "POST", payload)
        return status, json.loads(body)

    def new_session(self, **extra):
        payload = {"label": "Live Workshop", "location": "Room 204", "minutes": 60,
                   "step_seconds": 30}
        payload.update(extra)
        status, body = self.post_json("/api/sessions", payload)
        self.assertEqual(status, 201)
        return body["session"]


class ApiTests(ApiTestCase):
    def test_health_and_state(self):
        status, body = self.get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        self.new_session()
        status, blob = self.get_json("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(blob["session"]["label"], "Live Workshop")
        self.assertEqual(len(blob["code"]["current"]), 6)
        self.assertTrue(blob["chain"]["ok"])
        self.assertEqual(blob["roster"], [])
        self.assertIsNone(blob["session"].get("secret"))

    def test_index_renders_the_server_side_state(self):
        self.new_session()
        status, headers, body = request(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Live Workshop", body)
        self.assertNotIn("__BOOT_JSON__", body)
        self.assertIn("/static/app.js", body)

        for alias, view in (("/check-in", "checkin"), ("/roster", "roster"),
                            ("/anomalies", "anomalies"), ("/verify", "verify"),
                            ("/export", "export")):
            status, _h, page = request(self.base + alias)
            self.assertEqual(status, 200)
            self.assertIn('"view": "%s"' % view, page)

    def test_check_in_flow_over_http(self):
        session = self.new_session()
        _status, blob = self.get_json("/api/state?session=%d" % session["id"])
        code = blob["code"]["current"]

        status, first = self.post_json("/api/checkin", {"session_id": session["id"],
                                                        "ref": "P-301", "name": "Amina",
                                                        "code": code})
        self.assertEqual(status, 201)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["seq"], 1)

        status, again = self.post_json("/api/checkin", {"session_id": session["id"],
                                                        "ref": "P-301", "code": code})
        self.assertEqual(status, 201)
        self.assertEqual(again["status"], "duplicate")

        status, bad = self.post_json("/api/checkin", {"session_id": session["id"],
                                                      "ref": "P-302", "name": "Bruno",
                                                      "code": "AAAAAA"})
        self.assertEqual(status, 400)
        self.assertEqual(bad["status"], "rejected")

        _s, blob = self.get_json("/api/state?view=anomalies")
        rules = [f["rule"] for f in blob["anomalies"]]
        self.assertEqual(rules, ["duplicate_check_in"])
        self.assertEqual(blob["anomaly_counts"]["total"], 1)
        roster = {r["ref"]: r for r in blob["roster"]}
        self.assertTrue(roster["P-301"]["present"])

    def test_verify_endpoint_reports_a_tampered_row(self):
        session = self.new_session()
        _s, blob = self.get_json("/api/state")
        self.post_json("/api/checkin", {"session_id": session["id"], "ref": "P-401",
                                        "name": "Chen", "code": blob["code"]["current"]})
        status, ok = self.get_json("/api/verify")
        self.assertEqual(status, 200)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["checked"], 1)

        row = self.conn.execute("SELECT id FROM attendance ORDER BY seq LIMIT 1").fetchone()
        chain.tamper(self.conn, row["id"], "checked_in_at", "2020-01-01T00:00:00Z")

        status, broken = self.get_json("/api/verify")
        self.assertEqual(status, 200)
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["first_broken"]["attendance_id"], row["id"])
        self.assertEqual(broken["first_broken"]["reason"], "payload_mismatch")

        status, _h, page = request(self.base + "/?view=verify")
        self.assertIn('"ok": false', page)
        self.assertIn("payload_mismatch", page)
        self.assertIn('"view": "verify"', page)

    def test_export_routes(self):
        session = self.new_session()
        _s, blob = self.get_json("/api/state")
        self.post_json("/api/checkin", {"session_id": session["id"], "ref": "P-501",
                                        "name": "Daria", "code": blob["code"]["current"]})
        for name, expect in (("attendance.csv", "text/csv"), ("summary.csv", "text/csv"),
                             ("anomalies.csv", "text/csv"), ("all.json", "application/json")):
            status, headers, body = request(self.base + "/api/export/" + name)
            self.assertEqual(status, 200)
            self.assertIn(expect, headers["Content-Type"])
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
            self.assertTrue(body.strip(), "%s is empty" % name)
            if name != "anomalies.csv":          # there is nothing to flag here
                self.assertIn("P-501", body)

        status, _h, body = request(self.base + "/api/export/attendance.csv")
        self.assertIn("attendance_id,seq,session_id", body.split("\n")[0])

    def test_bad_requests_are_explained(self):
        status, body = self.get_json("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

        status, _h, raw = request(self.base + "/api/sessions", "POST", None)
        self.assertEqual(status, 400)                      # no label at all
        self.assertEqual(json.loads(raw)["error"], "invalid_session")

        malformed = urllib.request.Request(self.base + "/api/sessions", data=b"{not json",
                                           method="POST")
        malformed.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(malformed, timeout=10)
            self.fail("a malformed body should be refused")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.loads(exc.read().decode("utf-8"))["error"], "bad_json")

        status, body = self.post_json("/api/sessions", {"label": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_session")

    def test_two_clients_checking_in_at_once_keep_the_chain_valid(self):
        session = self.new_session(step_seconds=600)
        _s, blob = self.get_json("/api/state")
        code = blob["code"]["current"]
        results = []
        lock = threading.Lock()

        def worker(i):
            out = request(self.base + "/api/checkin", "POST",
                          {"session_id": session["id"], "ref": "P-6%02d" % i,
                           "name": "Person %d" % i, "code": code})
            with lock:
                results.append(out[0])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(results.count(201), 8)
        status, verified = self.get_json("/api/verify")
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["checked"], 8)
        _s, blob = self.get_json("/api/state")
        self.assertEqual(len(blob["roster"]), 8)
        self.assertEqual([row["seq"] for row in blob["chain_tail"]], list(range(8, 0, -1)))


if __name__ == "__main__":
    unittest.main()
