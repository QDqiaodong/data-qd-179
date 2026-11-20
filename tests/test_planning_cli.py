import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def invoke(source, output):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "registrar.planning_cli",
         "--source", str(source), "--output", str(output)],
        cwd=REPO, env=env, capture_output=True, text=True)


def planning_input():
    return {
        "as_of": "2026-06-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
        ],
        "students": [{"id": "s1"}, {"id": "s2"}],
        "program": {"required_courses": ["A", "B"],
                    "minimum_credits": {"STEM": 6}},
        "attempts": [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}],
        "planning": {
            "student_ids": ["s1", "s2"],
            "terms": [
                {"id": "t1", "start": "2026-09-01", "end": "2026-12-31",
                 "credit_limit": 3,
                 "offerings": [{"course_id": "A", "capacity": 1}]},
                {"id": "t2", "start": "2027-02-01", "end": "2027-05-31",
                 "credit_limit": 3,
                 "offerings": [{"course_id": "A", "capacity": 1},
                               {"course_id": "B", "capacity": 1}]},
            ],
        },
    }


class PlanningCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_console_script_is_declared(self):
        pyproject = (REPO / "pyproject.toml").read_text()
        self.assertIn('credit-plan = "registrar.planning_cli:main"', pyproject)

    def test_report_written_atomically_with_schema_one(self):
        source = self.dir / "in.json"
        output = self.dir / "nested" / "plan.json"
        source.write_text(json.dumps(planning_input()))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report),
                         {"schema_version", "as_of", "baseline_audit",
                          "terms", "students", "objective"})
        self.assertEqual(report["objective"]["graduates"], 2)
        self.assertIn("'graduates': 2", proc.stdout)
        # The baseline is the complete original audit report, untouched.
        self.assertIn("summary", report["baseline_audit"])

    def test_invalid_input_exits_2_and_creates_nothing(self):
        source = self.dir / "bad.json"
        output = self.dir / "out.json"
        bad = planning_input()
        bad["planning"]["terms"][0]["credit_limit"] = True
        source.write_text(json.dumps(bad))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("credit_limit", proc.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        source = self.dir / "in.json"
        output = self.dir / "out.json"
        source.write_text(json.dumps(planning_input()))
        output.write_text("KEEP ME")
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")

    def test_empty_plan_runs_successfully(self):
        source = self.dir / "empty.json"
        output = self.dir / "empty-out.json"
        data = planning_input()
        data["planning"]["student_ids"] = []
        data["planning"]["terms"] = []
        source.write_text(json.dumps(data))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["objective"], {
            "graduates": 0, "graduation_flags": [], "completion_indices": [],
            "planned_credits": 0, "enrollments": 0})


if __name__ == "__main__":
    unittest.main()
