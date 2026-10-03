"""Rotating code derivation, verification, grace window and expiry."""

from __future__ import annotations

import unittest

from rollcall import codes, util

from tests.support import at, BASE

SECRET = "a" * 64
SESSION = 7


class DerivationTests(unittest.TestCase):
    def test_code_is_deterministic_for_same_secret_and_step(self):
        self.assertEqual(codes.derive_code(SECRET, SESSION, 42),
                         codes.derive_code(SECRET, SESSION, 42))

    def test_code_changes_every_step_and_per_session(self):
        codes_now = [codes.derive_code(SECRET, SESSION, step) for step in range(40, 45)]
        self.assertEqual(len(set(codes_now)), 5)
        self.assertNotEqual(codes.derive_code(SECRET, SESSION, 42),
                            codes.derive_code(SECRET, SESSION + 1, 42))
        self.assertNotEqual(codes.derive_code(SECRET, SESSION, 42),
                            codes.derive_code("b" * 64, SESSION, 42))

    def test_alphabet_is_unambiguous_and_aliases_agree_with_it(self):
        for step in range(200):
            code = codes.derive_code(SECRET, SESSION, step)
            self.assertEqual(len(code), codes.CODE_LENGTH)
            for ch in code:
                self.assertIn(ch, codes.ALPHABET)
                self.assertNotIn(ch, "01OILZ2")
        for bad, good in codes._ALIASES.items():
            self.assertNotIn(bad, codes.ALPHABET)       # never alias a real code symbol
            self.assertIn(good, codes.ALPHABET)         # the alias target must be real
            self.assertEqual(codes.normalize(bad.lower()), good)

    def test_code_distribution_uses_most_of_the_alphabet(self):
        seen = set()
        for step in range(400):
            seen.update(codes.derive_code(SECRET, SESSION, step))
        self.assertGreaterEqual(len(seen), 25)

    def test_step_and_countdown_helpers(self):
        """Rotation steps are absolute: step = floor(unix_time / step_seconds)."""
        self.assertEqual(codes.step_for(75.9, 30), 2)
        self.assertEqual(codes.step_for(90, 30), 3)
        self.assertEqual(codes.seconds_left(75, 30), 15)
        self.assertEqual(codes.seconds_left(90, 30), 30)
        self.assertEqual(codes.step_window(2, 30), (60, 90))
        self.assertEqual(codes.step_for(util.epoch(at()), 30),
                         codes.step_for(util.epoch(at(seconds=0)) + 0, 30))


class VerificationTests(unittest.TestCase):
    def check(self, present_minutes=0, present_seconds=0, generated_minutes=0, back_steps=0):
        presented = codes.derive_code(SECRET, SESSION, codes.step_for(
            util.epoch(at(minutes=generated_minutes)), 30) - back_steps)
        return codes.check_code(SECRET, SESSION, presented,
                                util.epoch(at(minutes=present_minutes, seconds=present_seconds)), 30)

    def test_current_code_is_accepted(self):
        result = self.check(present_minutes=0, present_seconds=10)
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, codes.OK)
        self.assertFalse(result.stale)

    def test_previous_step_is_accepted_inside_grace_and_flagged_stale(self):
        result = self.check(present_minutes=0, present_seconds=40, generated_minutes=0)
        self.assertTrue(result.accepted)
        self.assertEqual(result.status, codes.STALE)
        self.assertTrue(result.stale)
        self.assertEqual(result.session_step - result.code_step, 1)

    def test_code_two_steps_old_is_expired(self):
        result = self.check(present_minutes=1, present_seconds=0, generated_minutes=0)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, codes.EXPIRED)

    def test_unrelated_code_is_invalid(self):
        result = codes.check_code(SECRET, SESSION, "AAAAAA", util.epoch(at()), 30)
        self.assertFalse(result.accepted)
        self.assertEqual(result.status, codes.INVALID)

    def test_wrong_length_is_malformed(self):
        result = codes.check_code(SECRET, SESSION, "ABC", util.epoch(at()), 30)
        self.assertEqual(result.status, codes.MALFORMED)
        self.assertIn("expected 6", result.message)

    def test_typing_lowercase_and_separators_still_works(self):
        code = codes.derive_code(SECRET, SESSION, codes.step_for(util.epoch(at()), 30)).lower()
        spaced = " ".join(code)
        result = codes.check_code(SECRET, SESSION, spaced, util.epoch(at()), 30)
        self.assertTrue(result.accepted)


if __name__ == "__main__":
    unittest.main()
