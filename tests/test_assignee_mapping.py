import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from process_voice_messages import (  # noqa: E402
    prepare_alias_map,
    prepare_assignee_map,
    resolve_assignee_ids,
)


class AssigneeMappingTests(unittest.TestCase):
    def test_prepare_assignee_map_normalizes_and_filters(self):
        raw = {
            "  Иван  ": "101",
            "Мария": ["202", None, "abc", 303],
            42: 404,
            "": 505,
        }

        result = prepare_assignee_map(raw)

        self.assertEqual(result["иван"], [101])
        self.assertEqual(result["мария"], [202, 303])
        self.assertNotIn("", result)
        self.assertNotIn(42, result)

    def test_resolve_assignee_ids_handles_conjunctions(self):
        assignee_map = prepare_assignee_map({
            "Иван": 1,
            "Мария": 2,
            "Петр": 3,
        })

        self.assertEqual(
            resolve_assignee_ids("Иван и Мария", assignee_map),
            [1, 2],
        )
        self.assertEqual(
            resolve_assignee_ids("Мария, Петр", assignee_map),
            [2, 3],
        )
        self.assertEqual(
            resolve_assignee_ids("Иван и/или Петр", assignee_map),
            [1, 3],
        )
        self.assertEqual(
            resolve_assignee_ids("Иван and/or Мария", assignee_map),
            [1, 2],
        )
        alias_map = prepare_alias_map({
            "Ваня": "Иван",
            "Маша": "Мария",
        })
        self.assertEqual(
            resolve_assignee_ids("Иван & Мария", assignee_map, alias_map),
            [1, 2],
        )
        self.assertEqual(
            resolve_assignee_ids("Ваня и Маша", assignee_map, alias_map),
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
