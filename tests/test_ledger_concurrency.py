"""进程间原子性、崩溃恢复、读者一致性与存储访问规模证据。

多进程用例经 tests/ledger_worker.py 以独立子进程驱动；故障注入点
（ledger._fault_hooks）只存在于测试进程，生产接口不需要任何手工清理。
规模用例通过替换 ledger._connection_factory 统计各表实际读取行数，
证明接收与旧批重试的访问量不随历史总量增长。
"""
import collections
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from registrar import ledger

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


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


def change(aid, revision, op="upsert", date="2026-02-20", mark=80):
    node = {"attempt_id": aid, "revision": revision, "student_id": "s1",
            "course_id": "A", "op": op}
    if op == "upsert":
        node["date"] = date
        node["mark"] = mark
    return node


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = str(self.dir / "store.db")
        ledger.initialize(self.store, init_input())

    def run_worker(self, node, *hook):
        batch_path = self.dir / f"batch-{len(list(self.dir.glob('batch-*')))}.json"
        batch_path.write_text(json.dumps(node))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + str(REPO)
        return subprocess.run(
            [sys.executable, "-m", "tests.ledger_worker", self.store,
             str(batch_path), *hook],
            cwd=REPO, env=env, capture_output=True, text=True)

    def test_concurrent_initialize_admits_exactly_one(self):
        store = str(self.dir / "race.db")
        source = self.dir / "init.json"
        source.write_text(json.dumps(init_input()))
        code = ("import json; from registrar import ledger; "
                f"ledger.initialize({store!r}, json.load(open({str(source)!r})))")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
                 for _ in range(2)]
        codes = sorted(proc.wait() for proc in procs)
        self.assertEqual(codes, [0, 1])  # 恰一个成功；败者拒绝且不破坏胜者
        self.assertEqual(ledger.audit(store)["known_seq"], 0)

    def test_concurrent_ingest_same_expected_seq_admits_exactly_one(self):
        first = {"batch_id": "p1", "expected_seq": 0,
                 "changes": [change("n1", 1)]}
        second = {"batch_id": "p2", "expected_seq": 0,
                  "changes": [change("n2", 1, date="2026-02-21")]}
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + str(REPO)
        procs = []
        for node in (first, second):
            path = self.dir / f"{node['batch_id']}.json"
            path.write_text(json.dumps(node))
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "tests.ledger_worker", self.store,
                 str(path), "sleep-begin", "0.5"],
                cwd=REPO, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True))
        results = [json.loads(proc.communicate()[0]) for proc in procs]
        winners = [r for r in results if r["ok"]]
        losers = [r for r in results if not r["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(winners[0]["receipt"]["seq"], 1)
        self.assertIn("expected_seq", losers[0]["error"])
        # 失败方不覆盖成功方：当前序号恰为 1，其 batch_id 未被抢占。
        self.assertEqual(ledger.audit(self.store)["known_seq"], 1)
        loser_id = "p1" if results[0] is losers[0] else "p2"
        retry = {"batch_id": loser_id, "expected_seq": 1,
                 "changes": [change("n3", 1, date="2026-02-22")]}
        receipt = ledger.ingest(self.store, retry)
        self.assertEqual(receipt["seq"], 2)

    def test_kill_before_commit_leaves_no_trace(self):
        node = {"batch_id": "doomed", "expected_seq": 0,
                "changes": [change("n1", 1)]}
        proc = self.run_worker(node, "exit-before-commit")
        self.assertEqual(proc.returncode, 42, proc.stderr)
        # 重新打开：序号未增加，事实与回执都不存在，同名批次可重新提交。
        self.assertEqual(ledger.audit(self.store)["known_seq"], 0)
        receipt = ledger.ingest(self.store, node)
        self.assertEqual(receipt["seq"], 1)
        self.assertEqual(ledger.ingest(self.store, node), receipt)

    def test_kill_after_commit_is_recovered_by_replay(self):
        node = {"batch_id": "committed", "expected_seq": 0,
                "changes": [change("n1", 1)]}
        proc = self.run_worker(node, "exit-after-commit")
        self.assertEqual(proc.returncode, 42, proc.stderr)
        # 提交已落盘但调用者未拿到响应：重开台账看到完整批次，
        # 用原批重试找回原回执。
        self.assertEqual(ledger.audit(self.store)["known_seq"], 1)
        receipt = ledger.ingest(self.store, node)
        self.assertEqual(receipt, {"batch_id": "committed", "seq": 1,
                                   "previous_seq": 0,
                                   "changed": [{"attempt_id": "n1",
                                                "before_revision": None,
                                                "after_revision": 1,
                                                "withdrawn": False}]})

    def test_raised_exception_rolls_back_completely(self):
        node = {"batch_id": "boom", "expected_seq": 0,
                "changes": [change("n1", 1)]}
        proc = self.run_worker(node, "raise-before-commit")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("injected fault", proc.stdout)
        self.assertEqual(ledger.audit(self.store)["known_seq"], 0)
        receipt = ledger.ingest(self.store, node)  # 同名批次修正后仍可用
        self.assertEqual(receipt["seq"], 1)

    def test_reader_never_sees_mixed_commits(self):
        node = {"batch_id": "slow", "expected_seq": 0,
                "changes": [change("late", 1)]}
        marker = self.dir / "marker"
        batch_path = self.dir / "slow.json"
        batch_path.write_text(json.dumps(node))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + str(REPO)
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.ledger_worker", self.store,
             str(batch_path), "sleep-before-commit", f"0.8,{marker}"],
            cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while not marker.exists():
            self.assertLess(time.time(), deadline, "writer never started")
            time.sleep(0.02)
        # 写者已插入事实但尚未提交：读者只能看到提交前的完整状态。
        during = ledger.audit(self.store)
        self.assertEqual(during["known_seq"], 0)
        self.assertNotIn("late", {v["attempt_id"] for v in during["versions"]})
        self.assertTrue(
            during["audit"]["students"][0]["graduation_eligible"])
        proc.communicate()
        after = ledger.audit(self.store)
        self.assertEqual(after["known_seq"], 1)
        self.assertIn("late", {v["attempt_id"] for v in after["versions"]})


class CountingConnection:
    """包装 sqlite3.Connection，按 FROM 子句的目标表统计取回的行数。"""

    def __init__(self, real):
        self._real = real
        self.reads = collections.Counter()

    def execute(self, sql, parameters=()):
        cursor = self._real.execute(sql, parameters)
        return CountingCursor(cursor, self.reads, sql)

    def __getattr__(self, name):
        return getattr(self._real, name)


class CountingCursor:
    def __init__(self, cursor, reads, sql):
        self._cursor = cursor
        self._reads = reads
        match = re.search(r"\bFROM\s+(\w+)", sql, re.IGNORECASE)
        self._table = match.group(1).lower() if match else None

    def _count(self, rows):
        if self._table:
            self._reads[self._table] += rows

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            self._count(1)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._count(len(rows))
        return rows

    def __iter__(self):
        rows = self._cursor.fetchall()
        self._count(len(rows))
        return iter(rows)


class ScaleEvidenceTests(unittest.TestCase):
    """合成历史远超活跃投影时，接收与重试的存储访问与历史总量无关。"""

    N_IDS = 1000
    BATCH_ROUNDS = (200, 200)  # 两轮历史，第二轮后总量翻倍
    CHANGES_PER_BATCH = 20

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = str(self.dir / "store.db")
        self.connections = []
        real_connect = sqlite3.connect

        def factory(*args, **kwargs):
            conn = CountingConnection(real_connect(*args, **kwargs))
            self.connections.append(conn)
            return conn

        ledger._connection_factory = factory
        self.addCleanup(setattr, ledger, "_connection_factory", sqlite3.connect)
        students = [{"id": f"s{i:04d}"} for i in range(self.N_IDS)]
        data = {
            "as_of": "2026-03-01",
            "courses": [{"id": "A", "credits": 3, "pass_mark": 60,
                         "category": "STEM", "prerequisites": []}],
            "students": students,
            "program": {"required_courses": [], "minimum_credits": {}},
            "attempts": [
                {"id": f"e{i:04d}", "student_id": students[i]["id"],
                 "course_id": "A", "date": "2026-01-10", "mark": 80}
                for i in range(self.N_IDS)],
        }
        ledger.initialize(self.store, data)
        self.revisions = {f"e{i:04d}": 0 for i in range(self.N_IDS)}
        self.seq = 0
        self.next_id = 0

    def grow_history(self, batches):
        for _ in range(batches):
            changes = []
            for _ in range(self.CHANGES_PER_BATCH):
                aid = f"e{self.next_id % self.N_IDS:04d}"
                self.next_id += 1
                self.revisions[aid] += 1
                changes.append({"attempt_id": aid,
                                "revision": self.revisions[aid],
                                "student_id": f"s{int(aid[1:]):04d}",
                                "course_id": "A", "op": "upsert",
                                "date": "2026-01-10", "mark": 80})
            self.seq += 1
            ledger.ingest(self.store, {
                "batch_id": f"h{self.seq}", "expected_seq": self.seq - 1,
                "changes": changes})

    def measured_batch(self, tag):
        """5 项：3 个新修订 + 2 个与历史完全重复的事实（跨批重复允许）。"""
        changes = []
        for i in range(3):
            aid = f"e{i:04d}"
            self.revisions[aid] += 1
            changes.append({"attempt_id": aid,
                            "revision": self.revisions[aid],
                            "student_id": f"s{i:04d}", "course_id": "A",
                            "op": "upsert", "date": "2026-01-10", "mark": 80})
        for i in range(2):
            aid = f"e{i + 100:04d}"
            changes.append({"attempt_id": aid, "revision": 1,
                            "student_id": f"s{int(aid[1:]):04d}",
                            "course_id": "A", "op": "upsert",
                            "date": "2026-01-10", "mark": 80})
        self.seq += 1
        return {"batch_id": tag, "expected_seq": self.seq - 1,
                "changes": changes}

    def measure(self, fn):
        del self.connections[:]
        result = fn()
        reads = collections.Counter()
        for conn in self.connections:
            reads.update(conn.reads)
        return result, reads

    def totals(self):
        conn = sqlite3.connect(self.store)
        try:
            facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            batches = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM current_state").fetchone()[0]
        finally:
            conn.close()
        return facts, batches, active

    def test_ingest_and_replay_access_is_history_independent(self):
        self.grow_history(self.BATCH_ROUNDS[0])
        facts1, batches1, active1 = self.totals()
        # 历史已明显超过活跃投影规模。
        self.assertGreaterEqual(facts1, 5 * active1)
        self.assertGreaterEqual(batches1, active1 // 5)
        first_batch = self.measured_batch("m1")
        _, reads_new1 = self.measure(
            lambda: ledger.ingest(self.store, first_batch))
        _, reads_replay1 = self.measure(
            lambda: ledger.ingest(self.store, first_batch))

        self.grow_history(self.BATCH_ROUNDS[1])
        facts2, batches2, active2 = self.totals()
        self.assertGreater(facts2, facts1 + 1000)
        second_batch = self.measured_batch("m2")
        _, reads_new2 = self.measure(
            lambda: ledger.ingest(self.store, second_batch))
        _, reads_replay2 = self.measure(
            lambda: ledger.ingest(self.store, second_batch))
        # 重访第一轮历史中的旧批次：精确定位，不扫描全部旧回执。
        _, reads_old_replay = self.measure(
            lambda: ledger.ingest(self.store, first_batch))

        # 新批接收：除允许整体读取的当前投影（current_state）外，
        # facts/batches 的读取量随批大小而非历史总量，两轮完全一致。
        self.assertEqual(reads_new1, reads_new2)
        self.assertLessEqual(reads_new1["facts"], len(first_batch["changes"]))
        self.assertLessEqual(reads_new1["batches"], 1)
        self.assertEqual(reads_new1["current_state"], active1)
        self.assertEqual(reads_new2["current_state"], active2)
        # 旧批精确重试：只读 meta 与一条 batches 记录，与历史规模无关。
        for reads in (reads_replay1, reads_replay2, reads_old_replay):
            self.assertEqual(reads["facts"], 0)
            self.assertEqual(reads["current_state"], 0)
            self.assertEqual(reads["batches"], 1)
        self.assertEqual(reads_replay1, reads_replay2)
        self.assertEqual(reads_replay1, reads_old_replay)

    def test_point_lookups_use_indexes(self):
        self.grow_history(5)
        conn = sqlite3.connect(self.store)
        try:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT student_id, course_id, op, date,"
                " mark FROM facts WHERE attempt_id = ? AND revision = ?",
                ("e0001", 1)).fetchall()
            detail = " ".join(row[-1] for row in plan)
            self.assertIn("SEARCH facts", detail)
            self.assertNotIn("SCAN facts", detail)
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT canon, receipt FROM batches"
                " WHERE batch_id = ?", ("h1",)).fetchall()
            detail = " ".join(row[-1] for row in plan)
            self.assertIn("SEARCH batches", detail)
            self.assertNotIn("SCAN batches", detail)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
