"""credit-reconcile 命令行测试：安装入口、退出码、原子新建与输入重排。"""
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
        [sys.executable, "-m", "registrar.reconciliation_cli",
         "--source", str(source), "--output", str(output)],
        cwd=REPO, env=env, capture_output=True, text=True)


def reconcile_input():
    return {
        "as_of": "2026-06-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
        ],
        "students": [{"id": "s1"}, {"id": "s2"}],
        "program": {"required_courses": ["A", "B"],
                    "minimum_credits": {"STEM": 6}},
        "attempts": [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-03-01", "mark": 90}],
        "reconciliation": {"waves": [
            {"id": "w1",
             "seats": [
                 {"id": "seat-a", "student_id": "s1", "course_id": "A",
                  "date": "2026-02-01"},
                 {"id": "seat-b", "student_id": "s2", "course_id": "A",
                  "date": "2026-02-01"},
             ],
             "sheets": [
                 {"id": "sh-hi", "mark": 90, "candidates": [
                     {"seat_id": "seat-a", "verified": False},
                     {"seat_id": "seat-b", "verified": False}]},
                 {"id": "sh-lo", "mark": 40, "candidates": [
                     {"seat_id": "seat-a", "verified": False},
                     {"seat_id": "seat-b", "verified": False}]},
             ]}]},
    }


class ReconcileCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, value):
        path = self.dir / "in.json"
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def test_console_script_is_declared(self):
        pyproject = (REPO / "pyproject.toml").read_text()
        self.assertIn(
            'credit-reconcile = "registrar.reconciliation_cli:main"', pyproject)

    def test_report_written_atomically_with_schema_one(self):
        source = self.write(reconcile_input())
        output = self.dir / "nested" / "report.json"
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(set(report),
                         {"schema_version", "as_of", "waves", "students"})
        rows = {s["student_id"]: s for s in report["students"]}
        self.assertEqual(rows["s1"]["graduation_status"], "needs_review")
        self.assertIn("'needs_review': 1", proc.stdout)
        self.assertIn("'waves': 1", proc.stdout)

    def test_invalid_input_exits_2_and_creates_nothing(self):
        bad = reconcile_input()
        bad["reconciliation"]["waves"][0]["sheets"][0]["mark"] = True
        source = self.write(bad)
        output = self.dir / "out.json"
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mark", proc.stderr)
        self.assertIn("credit-reconcile", proc.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        source = self.write(reconcile_input())
        output = self.dir / "out.json"
        output.write_text("KEEP ME")
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")

    def test_empty_waves_succeeds(self):
        data = reconcile_input()
        data["reconciliation"]["waves"] = []
        source = self.write(data)
        output = self.dir / "empty.json"
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["waves"], [])

    def test_reordered_input_yields_identical_report(self):
        data = reconcile_input()
        shuffled = json.loads(json.dumps(data))
        node = shuffled["reconciliation"]["waves"][0]
        node["seats"] = list(reversed(node["seats"]))
        node["sheets"] = list(reversed(node["sheets"]))
        for sh in node["sheets"]:
            sh["candidates"] = list(reversed(sh["candidates"]))
        out1 = self.dir / "a.json"
        out2 = self.dir / "b.json"
        p1 = invoke(self.write(data), out1)
        p2 = invoke(self.write(shuffled), out2)
        self.assertEqual((p1.returncode, p2.returncode), (0, 0))
        self.assertEqual(json.loads(out1.read_text()),
                         json.loads(out2.read_text()))


if __name__ == "__main__":
    unittest.main()
