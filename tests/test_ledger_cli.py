"""credit-ledger 命令行测试：安装入口、退出码、stdout 契约与原子新建报告。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def invoke(*args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run([sys.executable, "-m", "registrar.ledger_cli", *args],
                          cwd=REPO, env=env, capture_output=True, text=True)


def init_input():
    return {
        "as_of": "2026-03-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
        ],
        "students": [{"id": "s1"}],
        "program": {"required_courses": ["A", "B"],
                    "minimum_credits": {"STEM": 6}},
        "attempts": [
            {"id": "a", "student_id": "s1", "course_id": "A",
             "date": "2026-01-10", "mark": 80},
            {"id": "b", "student_id": "s1", "course_id": "B",
             "date": "2026-02-10", "mark": 80},
        ],
    }


class LedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = str(self.dir / "store.db")

    def write(self, name, value):
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return str(path)

    def init_store(self):
        source = self.write("init.json", init_input())
        proc = invoke("init", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_console_script_is_declared(self):
        pyproject = (REPO / "pyproject.toml").read_text()
        self.assertIn('credit-ledger = "registrar.ledger_cli:main"', pyproject)

    def test_init_outputs_json_and_zero(self):
        proc = self.init_store()
        self.assertEqual(json.loads(proc.stdout), {"seq": 0})
        # 已存在的 store 拒绝初始化并保留原内容。
        source = self.write("init2.json", init_input())
        proc = invoke("init", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_init_invalid_input_exits_2_without_store(self):
        bad = init_input()
        bad["attempts"][0]["mark"] = 101
        source = self.write("bad.json", bad)
        proc = invoke("init", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mark", proc.stderr)
        self.assertFalse(Path(self.store).exists())

    def test_ingest_receipt_and_replay_after_loss(self):
        self.init_store()
        node = {"batch_id": "X", "expected_seq": 0, "changes": [
            {"attempt_id": "a", "revision": 2, "student_id": "s1",
             "course_id": "A", "op": "withdraw"}]}
        source = self.write("batch.json", node)
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["seq"], 1)
        self.assertEqual(receipt["changed"], [
            {"after_revision": 2, "attempt_id": "a", "before_revision": 0,
             "withdrawn": True}])
        # 调用者未拿到 stdout 时用原批重试，找回同一回执。
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), receipt)

    def test_ingest_error_exit_2_without_pseudo_success(self):
        self.init_store()
        node = {"batch_id": "X", "expected_seq": 7, "changes": []}
        source = self.write("batch.json", node)
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("expected_seq", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_ingest_missing_store_is_error(self):
        source = self.write("batch.json",
                            {"batch_id": "X", "expected_seq": 0, "changes": []})
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("does not exist", proc.stderr)

    def test_audit_writes_report_atomically(self):
        self.init_store()
        output = self.dir / "nested" / "report.json"
        proc = invoke("audit", "--store", self.store, "--output", str(output))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(set(report), {"known_seq", "as_of", "versions", "audit"})
        self.assertEqual(report["known_seq"], 0)
        self.assertTrue(report["audit"]["students"][0]["graduation_eligible"])
        self.assertIn("known_seq", proc.stdout)
        # 不覆盖已有目标。
        proc = invoke("audit", "--store", self.store, "--output", str(output))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_audit_existing_output_is_preserved(self):
        self.init_store()
        output = self.dir / "report.json"
        output.write_text("KEEP ME")
        proc = invoke("audit", "--store", self.store, "--output", str(output))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")

    def test_audit_known_seq_and_as_of(self):
        self.init_store()
        node = {"batch_id": "X", "expected_seq": 0, "changes": [
            {"attempt_id": "a", "revision": 2, "student_id": "s1",
             "course_id": "A", "op": "withdraw"}]}
        source = self.write("batch.json", node)
        invoke("ingest", "--store", self.store, "--source", source)
        current = self.dir / "current.json"
        proc = invoke("audit", "--store", self.store, "--known-seq", "1",
                      "--as-of", "2026-03-01", "--output", str(current))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(current.read_text())
        self.assertFalse(report["audit"]["students"][0]["graduation_eligible"])
        history = self.dir / "history.json"
        proc = invoke("audit", "--store", self.store, "--known-seq", "0",
                      "--output", str(history))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(history.read_text())
        self.assertTrue(report["audit"]["students"][0]["graduation_eligible"])
        # 越界 known_seq 报错而不夹取。
        proc = invoke("audit", "--store", self.store, "--known-seq", "9",
                      "--output", str(self.dir / "out-of-range.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("known_seq", proc.stderr)
        proc = invoke("audit", "--store", self.store, "--as-of", "2026-3-1",
                      "--output", str(self.dir / "bad-date.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("canonical", proc.stderr)

    def test_duplicate_json_keys_and_non_finite_rejected(self):
        self.init_store()
        source = self.write(
            "dup.json",
            '{"batch_id": "X", "batch_id": "Y", "expected_seq": 0,'
            ' "changes": []}')
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("duplicate JSON key", proc.stderr)
        source = self.write(
            "nan.json",
            '{"batch_id": "X", "expected_seq": 0, "changes": ['
            '{"attempt_id": "a", "revision": 1, "student_id": "s1",'
            ' "course_id": "A", "op": "upsert", "date": "2026-01-10",'
            ' "mark": NaN}]}')
        proc = invoke("ingest", "--store", self.store, "--source", source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("non-finite", proc.stderr)

    def test_oversized_json_rejected(self):
        self.init_store()
        source = self.dir / "big.json"
        padding = " " * 8_100_000
        source.write_text('{"batch_id": "X", "expected_seq": 0, "changes": [],'
                          f'"pad": "{padding}"}}')
        proc = invoke("ingest", "--store", self.store, "--source", str(source))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("8 MB", proc.stderr)


if __name__ == "__main__":
    unittest.main()
