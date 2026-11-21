import json
import unittest
from pathlib import Path

from registrar.awards import review
from registrar.io import load

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class AwardsFixtureTests(unittest.TestCase):
    def test_v1_fixture_matches_documented_assignment(self):
        report = review(load(FIXTURES / "input_awards.json"))
        self.assertEqual(report["schema_version"], 1)
        rows = {row["student_id"]: row for row in report["students"]}
        # 未列入的 stu-bystander 不出现在报告中。
        self.assertEqual(set(rows), {"stu-full", "stu-short"})

        full = rows["stu-full"]
        self.assertEqual(full["primary"]["course_ids"], ["A", "B"])
        self.assertTrue(full["primary"]["eligible"])
        groups = {g["id"]: g for g in full["secondary"]["groups"]}
        self.assertEqual(groups["practice"]["course_ids"], ["D"])
        self.assertEqual(groups["project"]["course_ids"], ["C"])
        self.assertTrue(full["secondary"]["eligible"])

        short = rows["stu-short"]
        self.assertTrue(short["primary"]["eligible"])
        self.assertFalse(short["secondary"]["eligible"])
        groups = {g["id"]: g for g in short["secondary"]["groups"]}
        self.assertEqual(groups["practice"]["course_ids"], ["C"])
        self.assertEqual(groups["project"]["deficit"], 3)

    def test_v2_fixture_respects_source_restrictions_and_joint_success(self):
        report = review(load(FIXTURES / "input_awards_v2.json"))
        row = report["students"][0]
        sources = {p["course_id"]: p["source"]
                   for p in row["evidence_audit"]["selected_passes"]}
        self.assertEqual(sources["C"], "transfer")
        self.assertEqual(sources["A"], "exam")
        groups = {g["id"]: g for g in row["secondary"]["groups"]}
        # 转学分 C 只能进 any 组；exam 组只收考试来源的 A。
        self.assertEqual(groups["elective-any"]["course_ids"], ["C"])
        self.assertEqual(groups["elective-exam"]["course_ids"], ["A"])
        self.assertTrue(row["primary"]["eligible"])
        self.assertTrue(row["secondary"]["eligible"])
        self.assertEqual(
            row["evidence_audit"]["recognition_plan"]["option_ids"],
            ["recognize-c"])


if __name__ == "__main__":
    unittest.main()
