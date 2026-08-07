from __future__ import annotations

from datetime import date
import unittest

from main.term import (
    infer_current_term,
    is_valid_term,
    next_term,
    previous_term,
)


class TermTests(unittest.TestCase):
    def test_semantic_validation(self) -> None:
        self.assertTrue(is_valid_term("2025-2026-1"))
        for value in ("2025-2027-1", "2025-2026-3", "2025/2026/1", "bad-term"):
            with self.subTest(value=value):
                self.assertFalse(is_valid_term(value))

    def test_previous_and_next(self) -> None:
        self.assertEqual(previous_term("2025-2026-1"), "2024-2025-2")
        self.assertEqual(previous_term("2025-2026-2"), "2025-2026-1")
        self.assertEqual(next_term("2025-2026-1"), "2025-2026-2")
        self.assertEqual(next_term("2025-2026-2"), "2026-2027-1")

    def test_invalid_navigation_keeps_original_value(self) -> None:
        self.assertEqual(previous_term("bad-term"), "bad-term")
        self.assertEqual(next_term("bad-term"), "bad-term")

    def test_infer_current_term(self) -> None:
        cases = {
            date(2027, 2, 15): "2026-2027-1",
            date(2027, 2, 16): "2026-2027-2",
            date(2026, 7, 19): "2025-2026-2",
            date(2026, 7, 20): "2026-2027-1",
        }
        for today, expected in cases.items():
            with self.subTest(today=today):
                self.assertEqual(infer_current_term(today), expected)


if __name__ == "__main__":
    unittest.main()
