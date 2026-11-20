import json
import unittest
from pathlib import Path
from registrar.io import load
from registrar.service import run

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "input_v2.json"


class V2FixtureTests(unittest.TestCase):
    def setUp(self):
        self.report = run(load(FIXTURE))

    def test_schema_and_summary(self):
        self.assertEqual(self.report["schema_version"], 2)
        self.assertEqual(self.report["summary"], {
            "students": 2, "graduation_eligible": 1,
            "credited_units": 9, "blocked_attempts": 0})

    def test_holistic_choice_for_student_a(self):
        a, b = self.report["students"]
        self.assertEqual(a["recognition_plan"]["option_ids"], ["zz"])
        self.assertEqual(a["recognition_plan"]["charged_credits"], 3)
        self.assertEqual(a["credited_weighted_mark"], "230/3")
        sources = {p["course_id"]: p["source"] for p in a["selected_passes"]}
        self.assertEqual(sources, {"A": "transfer", "C": "exam", "D": "exam"})

    def test_student_b_future_option_unselectable_empty_plan(self):
        b = self.report["students"][1]
        self.assertEqual(b["recognition_plan"]["option_ids"], [])
        self.assertEqual(b["recognition_plan"]["charged_credits"], 0)
        self.assertEqual(b["selected_passes"], [])
        self.assertEqual(b["attempt_audit"][0]["status"], "failed")
        self.assertEqual(b["credited_weighted_mark"], None)


if __name__ == "__main__":
    unittest.main()
