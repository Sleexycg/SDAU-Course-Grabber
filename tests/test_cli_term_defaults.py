from __future__ import annotations

import unittest
from unittest.mock import patch

from main.cli import run_grab, run_query
from main.config import Settings


class CliTermDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(student_id="2024000001", password="password")

    def _assert_interactive_default(self, operation, title: str) -> None:
        inferred = "2026-2027-2"
        with (
            patch("main.cli.infer_current_term", return_value=inferred),
            patch("main.cli.select_term", return_value="invalid") as select,
            patch("main.cli.error"),
        ):
            self.assertEqual(operation(self.settings, interactive=True), 2)
        select.assert_called_once_with(inferred, title=title)

    def test_query_uses_date_inferred_default(self) -> None:
        self._assert_interactive_default(run_query, "查询选课结果")

    def test_grab_uses_date_inferred_default(self) -> None:
        self._assert_interactive_default(run_grab, "抢课模式")


if __name__ == "__main__":
    unittest.main()
