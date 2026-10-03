# Roll Call - design notes

Everything here is implemented in the standard library. The whole product is two
entry points (`app.py`, `cli.py`), one package (`rollcall/`), a hand-written UI in
`rollcall/static/` and one sqlite file.

## 1. Data model

```
sessions(id, label, location, lat, lon, opens_at, closes_at, step_seconds,
         secret, closed_at, created_at)
participants(id, ref UNIQUE, name, created_at)
attendance(id, seq UNIQUE, session_id, participant_id, kind, code_used, code_step,
           session_step, checked_in_at, recorded_at, source, note,
           prev_hash, entry_hash)
chain_state(id=1, last_seq, head_hash, updated_at)     -- head pointer
rejected_attempts(id, session_id, participant_ref, code, reason, at)
```

* **`sessions`** is the timetable. `opens_at`/`closes_at` define the window the
  *outside session window* rule (and the UI) uses. `step_seconds` is how often
  the check-in code rotates; sensible values are 10-60 s. `secret` is a per
  session 32 byte HMAC key generated with `secrets.token_hex`.
  `lat`/`lon` are optional: with them the impossible-travel rule becomes a real
  speed calculation, without them it falls back to room names.
* **`participants`** is deliberately account-free. A reference (`P-104`) plus a
  name is all the app knows; the reference is the identity that shows up in the
  register. There is no password, no e-mail, no device binding.
* **`attendance`** is the register and it is append-only by convention. One row
  per accepted check-in event: `kind` is `checkin`, or `duplicate` when the same
  person checks in twice (the second row is kept on purpose so the anomaly has
  evidence). `code_used` is what the participant typed and `code_step` is the
  rotation step that code belongs to, while `session_step` is the step live when
  the server accepted it - the pair is what makes *code reuse after rotation*
  detectable long after the fact. `checked_in_at` is the event time and
  `recorded_at` the time the server wrote the row; they differ only when data is
  imported or seeded.
* **`chain_state`** stores the head of the chain outside the log itself, so that
  *deleting* the newest rows does not go unnoticed.
* **`rejected_attempts`** keeps refuses (expired code, unknown session, closed
  session) so a facilitator can see that someone tried.

Money, photos, contact details and legal consent are deliberately out of scope.

## 2. Rotating codes

```
step            = unix_time // step_seconds
code(step)      = first 6 symbols of HMAC-SHA256(session_secret,
                      "rollcall/v1/code" || "<session_id>:<step>:" )   (streamed)
alphabet        = 3456789ABCDEFGHJKMNPQRSTUVWXY   (29 symbols, no 0/O, no 1/I/L, no 2/Z)
```

The HMAC output is consumed as a byte stream; bytes above `232` are skipped so
the mapping from byte to symbol is uniform (no modulo bias). 29^6 is about
5.9e8, so a code carries ~29.1 bits. Verification uses `hmac.compare_digest`.

**Acceptance rule.** A presented code is accepted if it matches the code of the
live step, or of the one step before it (`GRACE_STEPS = 1`). Anything older is
reported as `expired`, anything else as `invalid`, wrong length as `malformed`.
A code accepted from the previous step is stored with `code_step < session_step`
and is flagged `code_reuse_after_rotation` - it is evidence that somebody typed a
code from memory or from a photo after it rotated.

## 3. The hash chain

```
genesis      = "0" * 64
payload(row) = canonical JSON of { seq, session_id, participant_id, kind, code_used,
                                   code_step, session_step, checked_in_at,
                                   recorded_at, source }        (sorted keys, no spaces)
entry_hash   = sha256("rollcall/v1/entry" || prev_hash || payload(row))
prev_hash    = entry_hash of row seq-1  (genesis for seq 1)
```

`chain.append` runs inside `BEGIN IMMEDIATE`, so two simultaneous check-ins
cannot compute the same `seq` or the same `prev_hash`.

`chain.verify` walks rows in `seq` order from genesis and stops at the **first**
broken link, reporting the row, the reason and both hashes:

| reason | meaning |
|---|---|
| `payload_mismatch` | the row no longer hashes to its sealed `entry_hash` (fields edited, or a row forged) |
| `link_mismatch` | `prev_hash` does not match the previous row's `entry_hash` |
| `sequence_gap` | a row is missing from the middle (`seq` jump) |
| `head_mismatch` | the log is shorter or longer than the head pointer in `chain_state` |

`python3 cli.py verify` prints exactly that; `GET /api/verify` returns it as JSON.
`python3 cli.py tamper --row-id N` deliberately corrupts a row (red-team helper)
and the next verify run finds it.

**Honest limit.** The chain is not signed with a key that lives elsewhere. Anyone
who can rewrite the whole sqlite file can recompute the chain consistently; what
it reliably catches is a *surgical* edit - one attendance time, one ghost row,
one deleted row - which is the realistic way a register gets fudged. Anchoring the
daily head hash somewhere the operator cannot rewrite (a signed export mailed to
the organiser, a public gist, a second device) is the obvious next step and is
listed in the README.

## 4. Anomaly rules

All four rules are pure functions of the log; `detect_all` runs them in a fixed
order and each finding carries `rule`, `severity`, `reason`, `evidence_ids` and
the full `evidence` rows it was derived from. Same log in, same flags out.

| rule | severity | predicate (exactly) | evidence |
|---|---|---|---|
| `duplicate_check_in` | high | one `(session_id, participant_id)` pair with >= 2 attendance rows | all rows of that pair |
| `outside_session_window` | high | `checked_in_at < opens_at` or `checked_in_at > closes_at` | the row (+ `minutes_outside`) |
| `code_reuse_after_rotation` | warn | `code_step < session_step` (accepted inside the grace window), or the same `code_used` string appears in rows of two different sessions | the stale row, or all rows carrying that code |
| `impossible_travel` | high | consecutive check-ins of one participant in two sessions with **different** `location`: if both sessions have coordinates, `distance / gap > 120 km/h`; otherwise `gap < 30 minutes` | both rows (+ distance, gap, implied speed) |

Notes on the choices:

* `duplicate_check_in` is a *signal*, not a block: a reloaded form and a friend
  using someone else's reference look identical, so the app records both and lets
  the facilitator decide.
* the travel rule never fires for two sessions in the same room, and it prefers
  geometry over room names whenever coordinates exist. Degenerate cases (gap of
  0 minutes, distance 0 km) resolve deterministically.
* the cross-session branch of `code_reuse_after_rotation` can only trigger on a
  hand-crafted row (each session has its own secret, so codes are session-scoped
  by construction) - it exists as a cheap sanity check on imported or merged logs.

## 5. HTTP API

| method | path | purpose |
|---|---|---|
| GET | `/`, `/check-in`, `/?view=roster\|anomalies\|verify\|export` | the UI; the query string selects the screen so any state is addressable |
| GET | `/api/state?view=&session=` | everything the UI shows, in one blob (server-rendered into the page) |
| GET | `/api/health` | liveness + database name |
| GET | `/api/verify` | chain verification result |
| GET | `/api/anomalies?session=` | findings + counts |
| GET | `/api/export/attendance.csv` \| `summary.csv` \| `anomalies.csv` \| `all.json` | exports (optional `?session=`) |
| POST | `/api/sessions` | `{label, location, minutes, step_seconds, opens_at?, lat?, lon?}` |
| POST | `/api/sessions/<id>/close` | stop accepting check-ins |
| POST | `/api/checkin` | `{session_id, ref, name?, code}` |
| POST | `/api/participants` | `{ref, name}` (pre-register a person) |

Each request opens its own sqlite connection; writes serialise on the database
lock (`BEGIN IMMEDIATE`), which is what keeps the chain linear under the
threaded server. Verified by a test that fires eight simultaneous check-ins.

## 6. Exports

* `attendance.csv` - one row per attendance event, including `code_step`,
  `session_step`, `prev_hash` and `entry_hash`, so a spreadsheet line can be
  traced back into the chain.
* `summary.csv` - one row per person: sessions attended, entries, duplicates,
  first/last check-in, flags.
* `anomalies.csv` - one row per flag with the reason and the evidence row ids.
* `all.json` - sessions, participants, attendance, summary, anomalies and the
  chain verification in one machine-readable bundle (`schema: rollcall/v1`).

## 7. The demo scenario (`cli.py seed --demo`)

Built from *now* through the same service calls a real check-in uses, so the
seeded rows are sealed exactly like live ones:

* session 1 "Robotics Club - Build Night" (Room 204), 9 on-time check-ins;
* P-103 checks in twice -> `duplicate_check_in`;
* P-110 types the previous step's code -> `code_reuse_after_rotation`;
* P-107 checks in 12 minutes after the room closed -> `outside_session_window`;
* session 2 "Robotics Club - Sign-off Desk" (Room 118) opens 4 minutes after
  session 1, and P-104 appears in both -> `impossible_travel`;
* P-112 tries a code that expired six steps ago -> refused and logged in
  `rejected_attempts`.

13 sealed rows, 4 flags, and `cli.py verify` reports all 13 links intact.

## 8. Layout

```
app.py              python3 app.py  -> UI + JSON API on 127.0.0.1:8080
cli.py              seed / verify / anomalies / summary / export / tamper / code / sessions
rollcall/db.py      schema + connection
rollcall/codes.py   rotating code derivation and verification
rollcall/chain.py   append, verify, head pointer, tamper helper
rollcall/anomalies.py  the four rules
rollcall/service.py sessions, check-in, roster, summary, UI state
rollcall/exports.py CSV / JSON
rollcall/web.py     http.server routes
rollcall/static/    index.html, app.css, app.js (no framework, no CDN)
rollcall/demo.py    the deterministic demo scenario
tests/              64 unittest cases
tools/make_video.py builds the demo video from real screenshots + edge-tts
```
