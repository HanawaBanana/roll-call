"""Rotating check-in codes.

A code is derived with HMAC-SHA256 from ``(session secret, rotation step)`` and
rendered in a 29 symbol alphabet that omits visually ambiguous glyphs
("0/O", "1/I/L", "2/Z").  Codes are human typable on a phone; the facilitator's
(``0/O``, ``1/I/L``).  Codes are human typable on a phone; the facilitator's
screen shows one code at a time and it changes every ``step_seconds``.

Threat model (documented again in README.md):

* protects against an old code being typed minutes later -- it is invalid once
  the rotation step has passed the grace window;
* protects against guessing -- 6 characters of a 29 symbol alphabet is 29.1 bits,
  and the code is useless outside the session window;
* does NOT protect against a shoulder-surfer who reads the code off the screen
  and types it during the same step (that is what a human proctor is for);
* does NOT protect against someone who can read the sqlite file (the session
  secret is in it).  Rotating codes are anti-guessing hygiene, not
  cryptography for a hostile host.
"""

from __future__ import annotations

import hashlib
import hmac

#: Lenient alphabet: it contains neither 0/O nor 1/I/L nor 2/Z, so there is no
#: "is that a zero or a letter O" moment.  29 symbols = 29.1 bits for 6 characters.
ALPHABET = "3456789ABCDEFGHJKMNPQRSTUVWXY"
ALPHABET_SIZE = len(ALPHABET)
#: Largest multiple of ALPHABET_SIZE that fits in a byte: bytes above it are
#: discarded so the mapping stays uniform (no modulo bias).
_REJECT_AT = (256 // ALPHABET_SIZE) * ALPHABET_SIZE
DOMAIN = b"rollcall/v1/code"
# Lenient input: glyphs that are not in the alphabet, mapped to their nearest
# alphabet neighbour.  Keys are what a person may type, values are alphabet chars.
_ALIASES = {
    "O": "9", "0": "9",
    "I": "3", "1": "3", "L": "3",
    "Z": "8", "2": "8",
}

CODE_LENGTH = 6
STEP_SECONDS_DEFAULT = 30
# How many *extra* rotation steps behind "now" a code is still accepted, so a
# slow typist is not punished.  Anything older is expired and rejected.
GRACE_STEPS = 1

OK = "ok"
STALE = "stale"          # accepted, but the code already rotated (grace window)
EXPIRED = "expired"      # a real (older) code, too old to accept
INVALID = "invalid"      # does not match any recent step
MALFORMED = "malformed"  # wrong length / illegal characters


def step_for(moment_epoch, step_seconds: int) -> int:
    """Rotation step number for a POSIX timestamp."""
    return int(moment_epoch // int(step_seconds))


def normalize(code: str) -> str:
    """Uppercase, strip separators and apply the documented input leniency.

    The code alphabet excludes the ambiguous glyphs ``0 1 2 O I L Z``.  Because
    people still type them, the excluded characters are mapped onto the alphabet
    glyph they are most often confused with (``0/O`` -> ``9``, ``1/I/L`` -> ``3``,
    ``2/Z`` -> ``8``).  Anything else outside the alphabet stays as typed and the
    code is reported as malformed.
    """
    if code is None:
        return ""
    out = []
    for ch in str(code).upper():
        if ch in "- _\t":
            continue
        out.append(_ALIASES.get(ch, ch))
    return "".join(out)


def code_stream(secret_hex: str, session_id: int, step: int):
    """Endless deterministic byte stream: HMAC(secret, domain || session || step || block)."""
    key = bytes.fromhex(secret_hex)
    prefix = DOMAIN + b"%d:%d:" % (int(session_id), int(step))
    block = 0
    while True:
        for byte in hmac.new(key, prefix + str(block).encode("ascii"), hashlib.sha256).digest():
            yield byte
        block += 1


def derive_code(secret_hex: str, session_id: int, step: int, length: int = CODE_LENGTH) -> str:
    """Deterministic code for one rotation step (uniform over the alphabet)."""
    out = []
    for byte in code_stream(secret_hex, session_id, step):
        if byte >= _REJECT_AT:
            continue
        out.append(ALPHABET[byte % ALPHABET_SIZE])
        if len(out) == int(length):
            break
    return "".join(out)


class CodeCheck(object):
    """Result of verifying a presented code against a session."""

    __slots__ = ("status", "code_step", "session_step", "stale", "message")

    def __init__(self, status, code_step, session_step, stale, message):
        self.status = status
        self.code_step = code_step
        self.session_step = session_step
        self.stale = stale
        self.message = message

    @property
    def accepted(self) -> bool:
        return self.status in (OK, STALE)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "accepted": self.accepted,
            "code_step": self.code_step,
            "session_step": self.session_step,
            "stale": self.stale,
            "message": self.message,
        }


def check_code(secret_hex: str, session_id: int, code: str, moment_epoch,
               step_seconds: int, length: int = CODE_LENGTH,
               grace_steps: int = GRACE_STEPS) -> CodeCheck:
    """Verify *code* for the rotation step live at *moment_epoch*.

    Requires the code of the current step or of one of the ``grace_steps``
    previous steps.  Older codes are reported as ``expired``; codes that match
    nothing in the recent window are ``invalid``.
    """
    presented = normalize(code)
    if len(presented) != int(length) or any(ch not in ALPHABET for ch in presented):
        return CodeCheck(MALFORMED, None, None, False,
                         "expected %d characters from %s" % (length, ALPHABET))

    session_step = step_for(moment_epoch, step_seconds)
    for back in range(0, int(grace_steps) + 1):
        step = session_step - back
        expected = derive_code(secret_hex, session_id, step, length)
        if hmac.compare_digest(expected, presented):
            if back == 0:
                return CodeCheck(OK, step, session_step, False, "code is current")
            return CodeCheck(STALE, step, session_step, True,
                             "code belongs to rotation step %d; step %d is live"
                             % (step, session_step))

    # Was it a genuine code from a little earlier?  Then say "expired", not
    # "invalid" -- an honest reason beats a generic failure.
    for step in range(session_step - grace_steps - 1, session_step - grace_steps - 12, -1):
        if step < 0:
            break
        if hmac.compare_digest(derive_code(secret_hex, session_id, step, length), presented):
            return CodeCheck(EXPIRED, step, session_step, True,
                             "code expired %d rotation step(s) ago" % (session_step - step))
    return CodeCheck(INVALID, None, session_step, False, "code does not match this session")


def seconds_left(moment_epoch, step_seconds: int) -> int:
    """Seconds until the live code rotates."""
    return int(int(step_seconds) - (moment_epoch % int(step_seconds)))


def step_window(step: int, step_seconds: int):
    """``(start_epoch, end_epoch)`` of a rotation step."""
    start = int(step) * int(step_seconds)
    return start, start + int(step_seconds)
