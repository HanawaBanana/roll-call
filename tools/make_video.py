#!/usr/bin/env python3
"""Build the Roll Call demo video: real screenshots -> H.264 + edge-tts narration.

    python3 tools/make_video.py                # full run, writes docs/media/*
    python3 tools/make_video.py --keep         # keep the work directory
    python3 tools/make_video.py --no-narration # video only (no network needed)

What it does:

1. starts the real app on port 8782 with a scratch sqlite file,
2. seeds the deterministic demo history (``cli.py seed --demo``) and then *drives
   the running product*: opens a session, rotates a code, checks eight people in,
   triggers a duplicate, corrupts one row in sqlite on purpose, re-verifies,
   and exports CSV - taking a headless Chrome screenshot at every step,
3. every frame is held for a measured number of seconds, so the video timeline is
   the timeline of the demo and the narration can be pinned to scene starts,
4. generates the narration with edge-tts (en-US-AvaNeural, +20%),
5. muxes with ffmpeg into docs/media/roll_call_demo.mp4 (1280x800, H.264, 30fps).

Only the standard library is used here as well; Chrome, ffmpeg and edge-tts are
external commands that the script calls.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
PORT = 8782
SIZE = (1280, 800)
VOICE = "en-US-AvaNeural"
RATE = "+20%"
REPO_URL = "https://github.com/HanawaBanana/roll-call"

NARRATION = {
    "intro": "This is Roll Call: offline check-in and attendance for a small class, club or "
             "workshop. No accounts, no cloud. One Python file serves the app, one sqlite file "
             "holds the register.",
    "live": "I open a session for tonight's workshop. Roll Call shows a six character code and a "
            "countdown. The code is derived with H M A C from the session secret and the rotation "
            "step, it is verified on the server, and it expires when the step ends.",
    "checkin": "A participant opens the check-in page on their phone. No account, no install, no "
               "QR hardware. They type the code from the screen and their own reference.",
    "roster": "Eight people check in, one after another. The roster fills up live, and every "
              "accepted check-in is sealed into the hash chain with the code that was used.",
    "duplicate": "Then someone checks in twice: a reloaded form, or a friend trying it on. Roll "
                 "Call keeps the extra entry and flags the duplicate, naming the two rows that "
                 "prove it.",
    "history": "The seeded history shows the rest of the rules. One person appears in two "
               "different rooms one minute apart: impossible travel. Another checked in twelve "
               "minutes after the session closed. And one typed a code that had already rotated, "
               "accepted inside the grace window and flagged.",
    "verify": "Every row is sealed: each entry hash covers the previous hash and the canonical "
              "payload. Verifying walks the chain from the genesis block to the head. Everything "
              "intact.",
    "tamper": "Now I cheat. I edit one attendance time directly in the sqlite file, the way you "
              "would fudge a register. Verifying again finds the first broken link: that row, a "
              "payload mismatch, with the stored hash and the hash recomputed from the row.",
    "export": "The register exports as plain CSV: attendance, a per-participant summary and the "
              "flags, plus a JSON bundle that carries the chain hashes. No vendor, no lock in.",
    "closing": "Roll Call: rotating codes, a tamper-evident log, sixty four tests, and two "
               "commands to run it. Offline first attendance that a small club can really keep.",
}

# scene -> video seconds, so the narration is pinned to what is on screen
HOLDS = {
    "intro": [12.0],
    "live": [3.4] * 6,
    "checkin": [10.0],
    "roster": [2.3] * 8,
    "duplicate": [6.0, 6.0],
    "history": [18.0],
    "verify": [12.0],
    "tamper": [8.0, 8.0],
    "export": [12.0],
    "closing": [7.0, 7.0],
}
LEAD_MS = 350


# ---------------------------------------------------------------- http helpers

def http(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get_state(base, view="session", extra=""):
    return http("GET", "%s/api/state?view=%s%s" % (base, view, extra))


def wait_for_server(base, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http("GET", base + "/api/health")
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


# ---------------------------------------------------------------- recorder

class Recorder(object):
    """Collects real screenshots with their hold time; keeps scene start times."""

    def __init__(self, workdir, profile, base, log):
        self.workdir = workdir
        self.profile = profile
        self.base = base
        self.log = log
        self.frames = []          # [(png path, hold seconds)]
        self.marks = {}           # scene -> start seconds in the finished video
        self.now = 0.0
        self.count = 0
        self.scene = None

    def begin(self, scene):
        self.scene = scene
        self.marks[scene] = round(self.now, 3)
        print("[scene] %-10s starts at %6.1fs" % (scene, self.now))

    def shot(self, path, hold, label=""):
        url = path if path.startswith(("http://", "https://", "file://")) else self.base + path
        self.count += 1
        png = os.path.join(self.workdir, "frame_%03d.png" % self.count)
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--force-device-scale-factor=1", "--window-size=%d,%d" % SIZE,
               "--virtual-time-budget=2500", "--screenshot=" + png, url]
        started = time.time()
        # Headless Chrome occasionally dies with SIGTRAP when another browser is
        # starting at the same moment; three tries is plenty for a local run.
        last = None
        for attempt in range(3):
            try:
                if os.path.exists(png):
                    os.remove(png)
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120)
                last = None
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                last = exc
                print("        (chrome attempt %d failed: %s)" % (attempt + 1, exc))
                time.sleep(1.5)
        if last is not None:
            raise last
        size = os.path.getsize(png)
        if size < 8000:
            raise RuntimeError("screenshot %s looks empty (%d bytes) for %s" % (png, size, url))
        self.frames.append((png, float(hold)))
        self.now += float(hold)
        print("        frame %2d  t=%6.1fs  hold %4.1fs  %-42s %s (%d kB, %.1fs)"
              % (self.count, self.now, hold, label or path, self.scene, size // 1024,
                 time.time() - started))

    def total(self):
        return round(self.now, 3)


# ---------------------------------------------------------------- the demo run

def demo_run(rec, base, db_path, python):
    """Drive the running product and take the frames.  Returns facts for the props."""
    facts = {}

    # ---------------------------------------------------------------- 0 intro
    rec.begin("intro")
    rec.shot("/?view=session", HOLDS["intro"][0], "facilitator console, seeded history")

    # ---------------------------------------------------------------- 1 live session
    rec.begin("live")
    created = http("POST", base + "/api/sessions",
                   {"label": "Week 6 - Soldering Clinic", "location": "Room 204 (Main Lab)",
                    "minutes": 60, "step_seconds": 15})
    session = created["session"]
    facts["session_id"] = session["id"]
    first_code = created["code"]["current"]
    url = "/?view=session&session=%d" % session["id"]
    rec.shot(url, HOLDS["live"][0], "session opened, code + countdown")
    rotated = False
    for i in range(1, 6):
        # wait until the code really rotated so the frames show the rotation
        deadline = time.time() + 20
        while not rotated and time.time() < deadline:
            time.sleep(0.4)
            if get_state(base)["code"]["current"] != first_code:
                rotated = True
        rec.shot(url, HOLDS["live"][i],
                 "countdown ticks, code rolls over" if rotated and i > 2 else "countdown ticking")
    facts["code_rotated"] = rotated

    # ---------------------------------------------------------------- 2 check-in page
    rec.begin("checkin")
    roster = [("P-301", "Amina Belkacem"), ("P-302", "Bruno Tavares"), ("P-303", "Chen Wei"),
              ("P-304", "Daria Novak"), ("P-305", "Elif Kaya"), ("P-306", "Farid Haddad"),
              ("P-307", "Greta Lindqvist"), ("P-308", "Hugo Moreau")]
    code = get_state(base)["code"]["current"]
    first = http("POST", base + "/api/checkin",
                 {"session_id": session["id"], "ref": roster[0][0], "name": roster[0][1],
                  "code": code})
    facts["first_seq"] = first["seq"]
    rec.shot("/check-in?just=%s" % roster[0][0], HOLDS["checkin"][0],
             "participant page, receipt for the first check-in")

    # ---------------------------------------------------------------- 3 roster fills up
    rec.begin("roster")
    rec.shot("/?view=roster", HOLDS["roster"][0], "roster with 1 of 8 present")
    for i, (ref, name) in enumerate(roster[1:], start=1):
        code = get_state(base)["code"]["current"]
        http("POST", base + "/api/checkin",
             {"session_id": session["id"], "ref": ref, "name": name, "code": code})
        rec.shot("/?view=roster", HOLDS["roster"][i], "roster fills up: %d present" % (i + 1))
    facts["present"] = len(roster)

    # ---------------------------------------------------------------- 4 duplicate
    rec.begin("duplicate")
    code = get_state(base)["code"]["current"]
    duplicate = http("POST", base + "/api/checkin",
                     {"session_id": session["id"], "ref": roster[2][0], "code": code})
    facts["duplicate_status"] = duplicate["status"]
    facts["duplicate_ids"] = http("GET", base + "/api/anomalies")["anomalies"][0]["evidence_ids"]
    rec.shot("/?view=anomalies", HOLDS["duplicate"][0],
             "duplicate check-in flagged with both rows")
    rec.shot("/?view=anomalies", HOLDS["duplicate"][1], "anomaly evidence table")

    # ---------------------------------------------------------------- 5 seeded history
    rec.begin("history")
    all_flags = http("GET", base + "/api/anomalies")
    facts["rules"] = sorted({f["rule"] for f in all_flags["anomalies"]})
    facts["flag_count"] = all_flags["counts"]["total"]
    rec.shot("/?view=anomalies", HOLDS["history"][0],
             "all four anomaly rules with evidence rows")

    # ---------------------------------------------------------------- 6 verify
    rec.begin("verify")
    verified = http("GET", base + "/api/verify")
    facts["links"] = verified["checked"]
    facts["head"] = verified["head"]
    rec.shot("/?view=verify", HOLDS["verify"][0], "chain verified, %d links" % verified["checked"])

    # ---------------------------------------------------------------- 7 tamper
    rec.begin("tamper")
    target = http("GET", base + "/api/state?view=session")["recent"][-1]
    out = subprocess.run([python, "cli.py", "--db", db_path, "tamper",
                          "--row-id", str(target["id"]), "--field", "checked_in_at"],
                         cwd=PROJECT_DIR, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError("cli tamper failed: %s%s" % (out.stdout, out.stderr))
    facts["tampered_row"] = target["id"]
    facts["tamper_output"] = out.stdout.strip().splitlines()[0]
    rec.shot("/?view=verify", HOLDS["tamper"][0],
             "sqlite row %d edited behind the app's back" % target["id"])
    broken = http("GET", base + "/api/verify")
    facts["broken_reason"] = broken["first_broken"]["reason"]
    facts["broken_row"] = broken["first_broken"]["attendance_id"]
    rec.shot("/?view=verify", HOLDS["tamper"][1],
             "verify reports the first broken link")

    # ---------------------------------------------------------------- 8 export
    rec.begin("export")
    csv_text = urllib.request.urlopen(base + "/api/export/attendance.csv", timeout=15).read().decode()
    facts["csv_rows"] = csv_text.strip().count("\n")
    rec.shot("/?view=export&preview=attendance.csv", HOLDS["export"][0],
             "CSV export preview + download buttons")

    # ---------------------------------------------------------------- 9 closing
    rec.begin("closing")
    rec.shot("/?view=roster", HOLDS["closing"][0], "per-participant attendance summary")
    card = os.path.join(rec.workdir, "closing_card.html")
    with open(card, "w", encoding="utf-8") as handle:
        handle.write(closing_card_html(facts))
    rec.shot("file://" + card, HOLDS["closing"][1], "title card", )
    return facts


def closing_card_html(facts):
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    html,body{{margin:0;height:100%;background:#0e1116;color:#e7edf5;
      font-family:system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
      display:grid;place-items:center}}
    .card{{text-align:center;max-width:900px;padding:40px}}
    h1{{font-size:64px;margin:0 0 6px;letter-spacing:.02em}}
    .mark{{display:inline-grid;place-items:center;width:64px;height:64px;border-radius:16px;
      background:linear-gradient(140deg,#4ade80,#16a34a);color:#06240f;font-weight:800;
      font-size:26px;margin-bottom:22px}}
    .tag{{font-size:24px;color:#93a1b5;margin:0 0 26px}}
    .facts{{font-family:ui-monospace,Menlo,monospace;font-size:17px;color:#4ade80;line-height:2}}
    .foot{{margin-top:28px;font-size:16px;color:#6b7a90}}
    </style></head><body><div class="card">
    <div class="mark">RC</div>
    <h1>Roll&nbsp;Call</h1>
    <p class="tag">Offline check-in &amp; attendance for a small class, club or workshop</p>
    <p class="facts">rotating HMAC codes &middot; hash-chained log &middot; {links} links verified<br>
    {flags} anomaly flags across 4 deterministic rules<br>
    {rows} rows exported to CSV &middot; 64 unit tests &middot; MIT &middot; python3 standard library only</p>
    <p class="foot">{repo} &middot; one command: python3 app.py</p>
    </div></body></html>""".format(
        links=facts.get("links", 0), flags=facts.get("flag_count", 0),
        rows=facts.get("csv_rows", 0), repo=html.escape(REPO_URL))


# ---------------------------------------------------------------- media steps

def write_concat(frames, path):
    with open(path, "w", encoding="utf-8") as handle:
        for png, hold in frames:
            handle.write("file '%s'\nduration %.3f\n" % (png.replace("'", "'\\''"), hold))
        handle.write("file '%s'\n" % frames[-1][0].replace("'", "'\\''"))


def encode_video(frames, workdir, out_silent):
    concat = os.path.join(workdir, "frames.txt")
    write_concat(frames, concat)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", concat, "-vf",
           "scale=%d:%d,setsar=1,fps=30,format=yuv420p" % SIZE,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", out_silent]
    subprocess.run(cmd, check=True, timeout=600)
    return out_silent


def probe_duration(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], capture_output=True, text=True,
                         check=True)
    return float(out.stdout.strip())


def build_narration(marks, out_wav, workdir):
    """One mp3 per line with edge-tts, then a single padded, concatenated wav."""
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        print("edge-tts is not installed -> building the video without narration")
        return None
    import edge_tts

    async def synth(key, text):
        path = os.path.join(workdir, "n_%s.mp3" % key)
        await edge_tts.Communicate(text, VOICE, rate=RATE).save(path)
        return key, path

    async def synth_all():
        return [await synth(key, text) for key, text in NARRATION.items()]

    print("[audio] generating %d narration lines with %s" % (len(NARRATION), VOICE))
    clips = dict(asyncio.run(synth_all()))

    padded = []
    cursor_ms = 0
    for key in marks_order(marks):
        src = clips[key]
        dur = probe_duration(src)
        start_ms = int(marks[key] * 1000) + LEAD_MS
        if start_ms < cursor_ms + 150:              # never overlap two lines
            start_ms = cursor_ms + 150
        out = os.path.join(workdir, "p_%s.wav" % key)
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
                        "-af", "adelay=%d:all=1" % start_ms, "-ar", "44100", "-ac", "2",
                        "-c:a", "pcm_s16le", out], check=True, timeout=120)
        padded.append(out)
        cursor_ms = start_ms + int(dur * 1000)
        print("        %-10s starts %6.1fs  length %4.1fs" % (key, start_ms / 1000.0, dur))

    concat = os.path.join(workdir, "audio.txt")
    with open(concat, "w", encoding="utf-8") as handle:
        for path in padded:
            handle.write("file '%s'\n" % path.replace("'", "'\\''"))
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", concat, "-c", "copy", out_wav], check=True, timeout=300)
    return out_wav


def marks_order(marks):
    order = ["intro", "live", "checkin", "roster", "duplicate", "history", "verify", "tamper",
             "export", "closing"]
    return [key for key in order if key in marks]


def mux(silent, narration, out_mp4, duration):
    if narration is None:
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", silent,
                        "-c", "copy", out_mp4], check=True, timeout=300)
        return out_mp4
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", silent, "-i", narration,
           "-filter_complex", "[1:a]apad[a]", "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", "%.3f" % duration,
           "-movflags", "+faststart", out_mp4]
    subprocess.run(cmd, check=True, timeout=300)
    return out_mp4


def make_thumbnail(video, at_second, path):
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % at_second,
                    "-i", video, "-frames:v", "1", "-q:v", "3", path], check=True, timeout=120)
    return path


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="build the Roll Call demo video")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument("--no-narration", action="store_true")
    parser.add_argument("--workdir", default=None)
    a = parser.parse_args(argv)

    for tool in (CHROME, FFMPEG, FFPROBE):
        if not os.path.exists(tool):
            print("missing required tool: %s" % tool)
            return 1
    python = sys.executable

    workdir = a.workdir or tempfile.mkdtemp(prefix="rollcall-video-")
    os.makedirs(workdir, exist_ok=True)
    profile = os.path.join(workdir, "chrome-profile")
    db_path = os.path.join(workdir, "demo.sqlite3")
    base = "http://127.0.0.1:%d" % a.port
    print("[work] %s" % workdir)

    server_log = open(os.path.join(workdir, "server.log"), "wb")
    server = subprocess.Popen([python, "app.py", "--port", str(a.port), "--db", db_path],
                              cwd=PROJECT_DIR, stdout=server_log, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL)
    try:
        if not wait_for_server(base):
            raise RuntimeError("the app did not come up on %s" % base)
        print("[app]  up on %s" % base)
        seeded = subprocess.run([python, "cli.py", "--db", db_path, "seed", "--demo"],
                                cwd=PROJECT_DIR, capture_output=True, text=True, timeout=120)
        if seeded.returncode != 0:
            raise RuntimeError("seed failed: %s%s" % (seeded.stdout, seeded.stderr))
        print("[seed] demo history: %s" % seeded.stdout.strip().splitlines()[-1])

        rec = Recorder(workdir, profile, base, server_log)
        facts = demo_run(rec, base, db_path, python)
        duration = rec.total()
        print("[timeline] %d frames, %.1f seconds" % (len(rec.frames), duration))
        if not 110 <= duration <= 165:
            print("!! the brief asks for 110-165 seconds, this cut is %.1f" % duration)
            return 2

        silent = encode_video(rec.frames, workdir, os.path.join(workdir, "silent.mp4"))
        narration = None if a.no_narration else build_narration(rec.marks,
                                                                os.path.join(workdir, "narration.wav"),
                                                                workdir)
        media = os.path.join(PROJECT_DIR, "docs", "media")
        os.makedirs(media, exist_ok=True)
        out_mp4 = os.path.join(media, "roll_call_demo.mp4")
        mux(silent, narration, out_mp4, duration)
        make_thumbnail(out_mp4, rec.marks["verify"] + 5.0, os.path.join(media, "thumbnail.jpg"))
        shutil.copyfile(rec.frames[1][0], os.path.join(media, "screenshot.png"))

        final = probe_duration(out_mp4)
        size_mb = os.path.getsize(out_mp4) / 1048576.0
        print("\n[done] %s" % out_mp4)
        print("       duration %.1fs (target 110-165s), %.2f MB (<25 MB), %dx%d, 30 fps"
              % (final, size_mb, SIZE[0], SIZE[1]))
        print("       thumbnail %s" % os.path.join(media, "thumbnail.jpg"))
        print("       scene starts: %s" % json.dumps(rec.marks))
        print("       facts: %s" % json.dumps({k: v for k, v in facts.items()
                                               if k != "tamper_output"}))
        if size_mb >= 25:
            print("!! too big for a Devpost upload")
            return 3
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        server_log.close()
        if not a.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print("[work] kept at %s" % workdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
