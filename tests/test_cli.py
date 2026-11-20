import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from registrar.service import run
from tests.test_transfer_validation import base_input, with_transfer

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def invoke(source, output):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "registrar.cli",
         "--source", str(source), "--output", str(output)],
        cwd=REPO, env=env, capture_output=True, text=True)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v2_report_written_atomically(self):
        source = self.dir / "in.json"
        output = self.dir / "nested" / "report.json"
        source.write_text(json.dumps(with_transfer()))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 2)
        self.assertIn("recognition_plan", report["students"][0])
        self.assertIn("'students': 2", proc.stdout)

    def test_invalid_input_exits_2_and_creates_nothing(self):
        source = self.dir / "bad.json"
        output = self.dir / "out.json"
        bad = with_transfer(credit_limit=-3)
        source.write_text(json.dumps(bad))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("credit_limit", proc.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        source = self.dir / "in.json"
        output = self.dir / "out.json"
        source.write_text(json.dumps(with_transfer()))
        output.write_text("KEEP ME")
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")

    def test_v1_input_still_emits_schema_one(self):
        source = self.dir / "v1.json"
        output = self.dir / "v1-report.json"
        source.write_text(json.dumps(base_input()))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        for student in report["students"]:
            self.assertNotIn("recognition_plan", student)
            for row in student["selected_passes"]:
                self.assertNotIn("source", row)


if __name__ == "__main__":
    unittest.main()
