"""The HTTP layer: one stdlib server, one JSON API, one hand-written UI.

No framework, no CDN, no build step -- ``python3 app.py`` and the app is up on
localhost.  Every request opens its own sqlite connection, so the threaded
server stays honest under concurrent check-ins (sqlite serialises the writes).
"""

from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import anomalies, chain, db as _db, exports, service, util

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
BOOT_MARKER = "__BOOT_JSON__"

#: Friendly aliases so a participant can be handed a short URL.
VIEW_ALIASES = {
    "/check-in": "checkin",
    "/checkin": "checkin",
    "/roster": "roster",
    "/anomalies": "anomalies",
    "/verify": "verify",
    "/export": "export",
    "/": "session",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, code, message):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.message = message


def _int(value, default=None):
    if value in (None, "", "null"):
        return default
    return int(value)


class RollCallHandler(BaseHTTPRequestHandler):
    server_version = "RollCall/1.0"
    protocol_version = "HTTP/1.1"
    db_path = "rollcall.sqlite3"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, fmt, *args):  # keep the console readable
        if os.environ.get("ROLLCALL_VERBOSE"):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _connect(self):
        return _db.connect(self.db_path)

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload, default=str), extra=None)

    def _error(self, status, code, message):
        self._json({"error": code, "message": message}, status)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "request body must be JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return data

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        path, query = self._query()
        try:
            if path == "/api/health":
                return self._json({"ok": True, "db": os.path.basename(self.db_path),
                                   "version": "1.0.0"})
            if path == "/api/state":
                conn = self._connect()
                try:
                    blob = service.state(conn, query.get("session"), query.get("view", "session"))
                finally:
                    conn.close()
                return self._json(blob)
            if path == "/api/verify":
                conn = self._connect()
                try:
                    return self._json(chain.verify(conn))
                finally:
                    conn.close()
            if path == "/api/anomalies":
                conn = self._connect()
                try:
                    findings = anomalies.detect_all(conn, _int(query.get("session")))
                finally:
                    conn.close()
                return self._json({"counts": anomalies.counts(findings), "anomalies": findings})
            if path.startswith("/api/export/"):
                return self._export(path.rsplit("/", 1)[-1], query)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path in VIEW_ALIASES:
                # /?view=roster and /roster are the same screen; the query wins so a
                # screenshot (or a bookmark) can address any state by URL.
                view = query.get("view") or VIEW_ALIASES[path]
                if view not in set(VIEW_ALIASES.values()):
                    view = VIEW_ALIASES[path]
                return self._index(view, query)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._error(404, "not_found", "no route for %s" % path)
        except ApiError as exc:
            return self._error(exc.status, exc.code, exc.message)
        except service.ServiceError as exc:
            return self._error(400, exc.code, exc.message)
        except ValueError as exc:
            return self._error(400, "bad_request", str(exc))
        except BrokenPipeError:
            return

    def do_HEAD(self):
        return self.do_GET()

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        path, _query = self._query()
        try:
            body = self._body()
            conn = self._connect()
            try:
                if path == "/api/sessions":
                    session = service.create_session(
                        conn, body.get("label"), body.get("location", ""),
                        minutes=body.get("minutes", 60), step_seconds=body.get("step_seconds"),
                        opens_at=body.get("opens_at"), lat=body.get("lat"), lon=body.get("lon"))
                    return self._json({"session": dict(session),
                                       "code": service.code_info(session)}, 201)
                if path.startswith("/api/sessions/") and path.endswith("/close"):
                    session_id = path.split("/")[3]
                    session = service.close_session(conn, session_id)
                    return self._json({"session": dict(session)})
                if path == "/api/checkin":
                    result = service.check_in(
                        conn, body.get("session_id") or service.pick_session(conn)["id"],
                        body.get("ref"), body.get("name"), body.get("code"))
                    status = 201 if result["status"] in ("accepted", "duplicate") else 400
                    return self._json(result, status)
                if path == "/api/participants":
                    participant = service.ensure_participant(conn, body.get("ref"), body.get("name"))
                    return self._json({"participant": participant}, 201)
                if path == "/api/verify":
                    return self._json(chain.verify(conn))
                return self._error(404, "not_found", "no route for POST %s" % path)
            finally:
                conn.close()
        except ApiError as exc:
            return self._error(exc.status, exc.code, exc.message)
        except service.ServiceError as exc:
            return self._error(400, exc.code, exc.message)
        except ValueError as exc:
            return self._error(400, "bad_request", str(exc))
        except BrokenPipeError:
            return

    # ------------------------------------------------------------------ helpers

    def _export(self, name, query):
        conn = self._connect()
        try:
            session_id = _int(query.get("session"))
            if name == "attendance.csv":
                body, ctype = exports.attendance_csv(conn, session_id), "text/csv; charset=utf-8"
            elif name == "summary.csv":
                body, ctype = exports.summary_csv(conn, session_id), "text/csv; charset=utf-8"
            elif name == "anomalies.csv":
                body, ctype = exports.anomalies_csv(conn, session_id), "text/csv; charset=utf-8"
            elif name == "all.json":
                body, ctype = exports.json_bundle(conn, session_id), "application/json; charset=utf-8"
            else:
                return self._error(404, "not_found", "unknown export %s" % name)
        finally:
            conn.close()
        return self._send(200, body, ctype,
                          extra={"Content-Disposition": 'attachment; filename="%s"' % name})

    def _static(self, name):
        safe = os.path.normpath(name).lstrip("/")
        full = os.path.join(STATIC_DIR, safe)
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._error(404, "not_found", "no static file %s" % name)
        with open(full, "rb") as handle:
            body = handle.read()
        ext = os.path.splitext(full)[1].lower()
        return self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _index(self, view, query):
        conn = self._connect()
        try:
            blob = service.state(conn, query.get("session"), view)
        finally:
            conn.close()
        blob["just"] = query.get("just")
        blob["preview"] = query.get("preview")
        blob["station"] = query.get("station")
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as handle:
            html = handle.read()
        # The initial state is rendered server side into the page, so the very
        # first paint already shows real data (no flash of empty screen).
        html = html.replace(BOOT_MARKER, json.dumps(blob, default=str))
        return self._send(200, html, CONTENT_TYPES[".html"])


def build_handler(db_path: str):
    """Handler class bound to a database path."""
    return type("BoundRollCallHandler", (RollCallHandler,), {"db_path": db_path})


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """Create (but do not start) the threaded server."""
    server = ThreadingHTTPServer((host, int(port)), build_handler(db_path))
    server.daemon_threads = True
    return server


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = make_server(db_path, host, port)
    conn = _db.connect(db_path)
    try:
        sessions = _db.one(conn, "SELECT COUNT(*) AS n FROM sessions")["n"]
    finally:
        conn.close()
    banner = [
        "Roll Call - offline attendance, no accounts, no cloud",
        "  UI + API : http://%s:%d" % (host if host != "0.0.0.0" else "127.0.0.1", port),
        "  database : %s (%d session(s) on file)" % (os.path.abspath(db_path), sessions),
        "  stop     : Ctrl-C",
    ]
    print("\n".join(banner), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nRoll Call stopped.")
    finally:
        server.server_close()
