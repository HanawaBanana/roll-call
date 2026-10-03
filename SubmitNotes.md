# SubmitNotes - Arbiter Hacks V1 (General Track)

Filled in for the Devpost form. Everything here is copy-paste ready.

## Project name

**Roll Call**

## Tagline (one line)

Rotating codes, a hash-chained log and no accounts: offline check-in for a small
class, club or workshop that is actually better than paper.

## Built with (5 tags)

1. Python 3
2. SQLite
3. HMAC-SHA256
4. Vanilla JavaScript
5. HTML/CSS

(No paid API, no third-party service, no new account: the whole app is the Python
standard library, and it runs offline.)

## Description for the Devpost form (240 words)

Roll Call is an offline-first check-in and attendance app for a small class, club
or workshop. A facilitator opens a session and the screen shows a six-character
code that rotates every fifteen seconds; participants open the check-in page on
their phone, type the code plus their own reference, and they are on the roster.
No accounts, no e-mail, no QR hardware, no cloud.

It is built with nothing but the Python standard library. http.server serves the
hand-written UI - no framework, no CDN, phone-sized - and a JSON API, while
sqlite3 keeps the state in one file. Check-in codes are derived with HMAC-SHA256
from the session secret and the rotation step, verified server-side with a
one-step grace window, so an old code is refused and a late one is flagged. Every
accepted check-in is sealed into a hash chain (prev_hash -> payload -> sha256); a
verify command walks the log and prints the first broken link. Four deterministic
anomaly rules - duplicate check-ins, impossible travel, code reuse after rotation,
and check-ins outside the session window - each report their reason and the exact
rows they came from. CSV and JSON exports and per-participant summaries fall out of
the same log. Sixty-four unit tests cover it, and the demo video corrupts a row in
sqlite to show verify catching it.

Next: anchor the daily chain head off-device, import an expected roster, and add
room codes so travel anomalies do not need coordinates.

## Links

* Public repository: https://github.com/HanawaBanana/roll-call
* Demo video (local file, upload to YouTube/Vimeo is done from the main session):
  `docs/media/roll_call_demo.mp4` - 2:25, 1280x800, H.264 + narration
* Thumbnail: `docs/media/thumbnail.jpg`
* Screenshot used in the README: `docs/media/screenshot.png`

## Judge-facing quick check

```bash
git clone https://github.com/HanawaBanana/roll-call && cd roll-call
python3 -m unittest discover -s tests     # 64 tests, no dependencies
python3 app.py --demo                     # http://127.0.0.1:8080
python3 cli.py seed --demo && python3 cli.py verify
python3 cli.py tamper --row-id 4 && python3 cli.py verify   # chain broken, row 4 named
```

Python 3.9+, standard library only, MIT licensed.
