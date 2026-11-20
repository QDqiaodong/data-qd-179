import json
import unittest
from pathlib import Path
from registrar.service import run


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class CreditTests(unittest.TestCase):
    def test_invalid_early_pass_does_not_count_and_graduation_is_joint(self):
        result = run(fixture())
        a, b = result["students"]
        self.assertEqual(result["summary"], {"students": 2, "graduation_eligible": 1,
                         "credited_units": 12, "blocked_attempts": 1})
        self.assertTrue(a["graduation_eligible"])
        self.assertEqual(a["credits"], 9)
        self.assertEqual(a["credited_weighted_mark"], "740/9")
        self.assertEqual(a["attempt_audit"][2]["status"], "blocked_prerequisite")
        self.assertEqual(b["credit_deficits"], {"STEM": 4, "arts": 2})
        self.assertEqual(b["eligible_uncredited_courses"], ["PHYS", "WRITE"])

    def test_same_day_prerequisite_is_not_earlier(self):
        data = fixture()
        data["attempts"][3]["date"] = "2026-01-20"
        data["attempts"] = data["attempts"][:4]
        a = run(data)["students"][0]
        self.assertEqual(a["credits"], 5)
        self.assertEqual(a["missing_required"], ["PHYS"])

    def test_retakes_choose_best_without_double_credits(self):
        data = fixture()
        data["attempts"].append({"id": "a6", "student_id": "synthetic-A", "course_id": "MATH",
                                "date": "2026-03-20", "mark": 95})
        a = run(data)["students"][0]
        self.assertEqual(a["credits"], 9)
        self.assertEqual(a["selected_passes"][0]["attempt_id"], "a6")
        self.assertEqual(a["credited_weighted_mark"], "785/9")

    def test_asof_excludes_future_evidence(self):
        data = fixture()
        data["as_of"] = "2026-02-01"
        a = run(data)["students"][0]
        self.assertEqual(a["ignored_future_attempts"], 2)
        self.assertEqual(a["credits"], 2)
        self.assertEqual(a["eligible_uncredited_courses"], ["MATH"])

    def test_cycle_and_duplicate_exam_rejected(self):
        data = fixture()
        data["courses"][0]["prerequisites"] = ["PHYS"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            run(data)
        data = fixture()
        copy = dict(data["attempts"][0], id="another")
        data["attempts"].append(copy)
        with self.assertRaisesRegex(ValueError, "student/course/day"):
            run(data)

    def test_v1_report_shape_is_unchanged_without_transfer(self):
        result = run(fixture())
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(set(result), {"schema_version", "as_of", "students", "summary"})
        self.assertEqual(
            set(result["students"][0]),
            {"student_id", "selected_passes", "attempt_audit",
             "ignored_future_attempts", "credits", "credits_by_category",
             "credited_weighted_mark", "missing_required", "credit_deficits",
             "graduation_eligible", "eligible_uncredited_courses"})
        self.assertEqual(
            set(result["students"][0]["selected_passes"][0]),
            {"course_id", "attempt_id", "mark", "credits"})
        self.assertEqual(
            result["summary"],
            {"students": 2, "graduation_eligible": 1,
             "credited_units": 12, "blocked_attempts": 1})

    def test_v1_validation_errors_remain_hard_failures(self):
        data = fixture()
        data["as_of"] = "2026-02-30"
        with self.assertRaises(ValueError):
            run(data)
        data = fixture()
        data["attempts"][0]["mark"] = True
        with self.assertRaisesRegex(ValueError, "mark"):
            run(data)
