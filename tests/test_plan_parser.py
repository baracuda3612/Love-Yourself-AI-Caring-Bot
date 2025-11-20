import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.plan_parser import parse_plan_request


class PlanParserTests(unittest.TestCase):
    def test_hyphenated_duration_and_time(self):
        result = parse_plan_request("/plan 7-денний челендж підтримки о 22:00")
        self.assertEqual(result.days, 7)
        self.assertEqual((result.hour, result.minute), (22, 0))
        self.assertEqual(result.hours_list, ["22:00"])
        self.assertEqual(result.goal, "челендж підтримки")

    def test_defaults_when_missing(self):
        result = parse_plan_request("/plan підтримка добробуту")
        self.assertEqual(result.days, 7)
        self.assertEqual((result.hour, result.minute), (21, 0))
        self.assertEqual(result.tasks_per_day, 1)
        self.assertEqual(result.hours_list, ["21:00"])
        self.assertEqual(result.goal, "підтримка добробуту")

    def test_custom_days_and_tasks(self):
        result = parse_plan_request("/plan покращити сон на 10 днів 2 завдання на день")
        self.assertEqual(result.days, 10)
        self.assertEqual(result.tasks_per_day, 2)
        self.assertEqual(result.goal, "покращити сон")

    def test_multiple_hours_and_at_symbols(self):
        result = parse_plan_request("/plan стабілізація 14 days @08:00 @14:00 @21:00")
        self.assertEqual(result.days, 14)
        self.assertEqual(result.tasks_per_day, 3)
        self.assertEqual(result.hours_list, ["08:00", "14:00", "21:00"])
        self.assertEqual(result.goal, "стабілізація")


if __name__ == "__main__":
    unittest.main()
