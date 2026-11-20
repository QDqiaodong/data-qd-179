"""台账核心语义测试：时间轴、重放、批原子性、身份与修订冲突、回执口径。"""
import unittest
from pathlib import Path

from registrar import ledger
from registrar.io import load
from registrar.service import run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def walkthrough_input():
    """规格用例：A 无先修、B 先修 A，STEM 各 3 学分，及格 60，方案要求 A、B。"""
    return {
        "as_of": "2026-03-01",
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
            {"id": "a", "student_id": "s1", "course_id": "A",
             "date": "2026-01-10", "mark": 80},
            {"id": "b", "student_id": "s1", "course_id": "B",
             "date": "2026-02-10", "mark": 80},
        ],
    }


def chain_input():
    """A→B→C 先修链，用于级联失效/解锁测试。"""
    return {
        "as_of": "2026-06-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
            {"id": "C", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["B"]},
        ],
        "students": [{"id": "s1"}],
        "program": {"required_courses": ["A", "B", "C"],
                    "minimum_credits": {"STEM": 9}},
        "attempts": [
            {"id": "a", "student_id": "s1", "course_id": "A",
             "date": "2026-01-10", "mark": 80},
            {"id": "b", "student_id": "s1", "course_id": "B",
             "date": "2026-02-10", "mark": 80},
            {"id": "c", "student_id": "s1", "course_id": "C",
             "date": "2026-03-10", "mark": 80},
        ],
    }


def v2_input():
    """v2 台账输入：更正会改变最优认定方案。"""
    return {
        "as_of": "2026-06-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "C", "credits": 4, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
            {"id": "D", "credits": 2, "pass_mark": 60, "category": "arts",
             "prerequisites": []},
        ],
        "students": [{"id": "s1"}],
        "program": {"required_courses": ["A", "C"],
                    "minimum_credits": {"STEM": 7, "arts": 2}},
        "attempts": [
            {"id": "d1", "student_id": "s1", "course_id": "D",
             "date": "2026-02-01", "mark": 70},
            {"id": "c1", "student_id": "s1", "course_id": "C",
             "date": "2026-03-01", "mark": 80},
        ],
        "transfer": {
            "credit_limit": 3,
            "evidence": [{"id": "e1", "student_id": "s1", "date": "2026-01-01"}],
            "options": [
                {"id": "aa", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["B"], "effective_date": "2026-01-31"},
                {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["A"], "effective_date": "2026-01-31"},
            ],
        },
    }


def change(aid, revision, sid="s1", cid="A", op="upsert", date="2026-02-20",
           mark=80):
    node = {"attempt_id": aid, "revision": revision, "student_id": sid,
            "course_id": cid, "op": op}
    if op == "upsert":
        node["date"] = date
        node["mark"] = mark
    return node


def batch(batch_id, expected_seq, changes):
    return {"batch_id": batch_id, "expected_seq": expected_seq,
            "changes": changes}


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / "store.db")

    def init(self, data=None):
        return ledger.initialize(self.store, data or walkthrough_input())

    def ingest(self, *args, **kwargs):
        return ledger.ingest(self.store, *args, **kwargs)

    def audit(self, **kwargs):
        return ledger.audit(self.store, **kwargs)

    def student(self, report, sid="s1"):
        rows = {row["student_id"]: row for row in report["audit"]["students"]}
        return rows[sid]


class WalkthroughTests(LedgerTestCase):
    """规格书中的完整时间轴用例。"""

    def test_spec_walkthrough(self):
        self.assertEqual(self.init(), {"seq": 0})
        self.assertTrue(self.student(self.audit())["graduation_eligible"])

        receipt_x = self.ingest(batch("X", 0, [
            change("a", 2, op="withdraw")]))
        self.assertEqual(receipt_x, {
            "batch_id": "X", "seq": 1, "previous_seq": 0,
            "changed": [{"attempt_id": "a", "before_revision": 0,
                         "after_revision": 2, "withdrawn": True}]})
        after_x = self.student(self.audit())
        self.assertFalse(after_x["graduation_eligible"])
        statuses = {row["attempt_id"]: row["status"]
                    for row in after_x["attempt_audit"]}
        self.assertEqual(statuses, {"b": "blocked_prerequisite"})

        # Y 带来更旧的 revision=1：不能复活 a，changed 为空，但仍取得 seq=2。
        receipt_y = self.ingest(batch("Y", 1, [
            change("a", 1, date="2026-01-10", mark=90)]))
        self.assertEqual(receipt_y["changed"], [])
        self.assertEqual(receipt_y["seq"], 2)
        self.assertFalse(self.student(self.audit())["graduation_eligible"])

        # seq=0 的历史查询依然可毕业。
        history = self.audit(known_seq=0)
        self.assertEqual(history["known_seq"], 0)
        self.assertTrue(self.student(history)["graduation_eligible"])

        # Z 把 a 恢复为与 b 同日的 95 分：a 计入学分，b 仍被同日阻断。
        receipt_z = self.ingest(batch("Z", 2, [
            change("a", 3, date="2026-02-10", mark=95)]))
        self.assertEqual(receipt_z["changed"], [
            {"attempt_id": "a", "before_revision": 2,
             "after_revision": 3, "withdrawn": False}])
        after_z = self.student(self.audit())
        self.assertFalse(after_z["graduation_eligible"])
        statuses = {row["attempt_id"]: row["status"]
                    for row in after_z["attempt_audit"]}
        self.assertEqual(statuses, {"a": "passed", "b": "blocked_prerequisite"})
        self.assertEqual(after_z["credits"], 3)

        # 改成严格更早的日期才解锁 b。
        self.ingest(batch("Z2", 3, [change("a", 4, date="2026-01-15", mark=95)]))
        self.assertTrue(self.student(self.audit())["graduation_eligible"])

        # X 在 Z 之后重试仍返回其 seq=1 原回执；修改 X 的内容则拒绝。
        self.assertEqual(self.ingest(batch("X", 0, [
            change("a", 2, op="withdraw")])), receipt_x)
        with self.assertRaisesRegex(ValueError, "batch_id conflict"):
            self.ingest(batch("X", 0, [change("a", 3, op="withdraw")]))
        with self.assertRaisesRegex(ValueError, "batch_id conflict"):
            self.ingest(batch("X", 4, [  # expected_seq 也属于重放身份
                change("a", 2, op="withdraw")]))


class InitializeTests(LedgerTestCase):
    def test_existing_store_is_rejected_and_preserved(self):
        self.init()
        before = Path(self.store).read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.init()
        self.assertEqual(Path(self.store).read_bytes(), before)
        self.assertEqual(self.audit()["known_seq"], 0)

    def test_existing_plain_file_is_rejected(self):
        Path(self.store).write_text("not a ledger")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.init()
        self.assertEqual(Path(self.store).read_text(), "not a ledger")

    def test_failed_init_leaves_no_openable_store(self):
        data = walkthrough_input()
        data["attempts"].append({"id": "bad", "student_id": "ghost",
                                 "course_id": "A", "date": "2026-01-10",
                                 "mark": 80})
        with self.assertRaises(ValueError):
            self.init(data)
        self.assertFalse(Path(self.store).exists())
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ledger.ingest(self.store, batch("b1", 0, []))
        # 修正输入后同一路径仍可成功初始化。
        self.assertEqual(self.init(), {"seq": 0})

    def test_invalid_store_file_is_not_a_ledger(self):
        Path(self.store).write_text("garbage")
        with self.assertRaisesRegex(ValueError, "invalid ledger store"):
            ledger.audit(self.store)

    def test_original_attempts_are_revision_zero(self):
        self.init()
        versions = {v["attempt_id"]: v for v in self.audit()["versions"]}
        self.assertEqual(versions["a"],
                         {"attempt_id": "a", "revision": 0, "withdrawn": False})
        self.assertEqual(versions["b"],
                         {"attempt_id": "b", "revision": 0, "withdrawn": False})

    def test_empty_attempts_and_v2_schema(self):
        data = walkthrough_input()
        data["attempts"] = []
        self.init(data)
        result = self.audit()
        self.assertEqual(result["versions"], [])
        self.assertEqual(result["audit"]["schema_version"], 1)
        store2 = str(Path(self.tmp.name) / "v2.db")
        ledger.initialize(store2, v2_input())
        self.assertEqual(ledger.audit(store2)["audit"]["schema_version"], 2)


class BatchValidationTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def assert_rejected(self, node, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            self.ingest(node)
        self.assertEqual(self.audit()["known_seq"], 0)  # 失败不消耗 seq

    def test_batch_structure(self):
        self.assert_rejected([], "batch must be an object")
        self.assert_rejected({"expected_seq": 0, "changes": []}, "batch_id")
        self.assert_rejected({"batch_id": "b", "changes": []}, "expected_seq")
        self.assert_rejected({"batch_id": "b", "expected_seq": 0}, "changes")
        self.assert_rejected({"batch_id": "b", "expected_seq": 0,
                              "changes": [], "extra": 1}, "undefined fields")
        self.assert_rejected({"batch_id": "  ", "expected_seq": 0,
                              "changes": []}, "batch_id")
        self.assert_rejected({"batch_id": "b", "expected_seq": -1,
                              "changes": []}, "expected_seq")
        self.assert_rejected({"batch_id": "b", "expected_seq": True,
                              "changes": []}, "expected_seq")
        self.assert_rejected({"batch_id": "b", "expected_seq": 0,
                              "changes": {}}, "changes must be an array")

    def test_changes_limit(self):
        changes = [change(f"n{i}", 1) for i in range(1001)]
        self.assert_rejected(batch("b", 0, changes), "at most 1000")

    def test_change_fields(self):
        base = batch("b", 0, [change("n", 1)])
        del base["changes"][0]["mark"]
        self.assert_rejected(base, "mark is required")
        node = change("n", 1, op="withdraw")
        node["date"] = "2026-01-10"
        self.assert_rejected(batch("b", 0, [node]), "undefined fields")
        node = change("n", 1)
        node["op"] = "erase"
        self.assert_rejected(batch("b", 0, [node]), "op")
        node = change("n", 1)
        node["extra"] = 1
        self.assert_rejected(batch("b", 0, [node]), "undefined fields")
        node = change("n", 1)
        del node["student_id"]
        self.assert_rejected(batch("b", 0, [node]), "student_id is required")

    def test_integer_and_date_rules(self):
        self.assert_rejected(batch("b", 0, [change("n", True)]), "revision")
        self.assert_rejected(batch("b", 0, [change("n", 0)]), "revision")
        self.assert_rejected(batch("b", 0, [change("n", -2)]), "revision")
        self.assert_rejected(batch("b", 0, [change("n", 1, mark=True)]), "mark")
        self.assert_rejected(batch("b", 0, [change("n", 1, mark=101)]),
                             "exceeds 100")
        self.assert_rejected(batch("b", 0, [change("n", 1, date="2026-1-10")]),
                             "canonical")
        self.assert_rejected(batch("b", 0, [change("n", 1, mark=3.5)]), "mark")

    def test_duplicate_pair_within_changes(self):
        dup = [change("n", 1), change("n", 1)]
        self.assert_rejected(batch("b", 0, dup), "duplicate")
        dup = [change("n", 1), change("n", 1, mark=90)]
        self.assert_rejected(batch("b", 0, dup), "duplicate")

    def test_unknown_student_or_course(self):
        self.assert_rejected(batch("b", 0, [change("n", 1, sid="ghost")]),
                             "unknown change student")
        self.assert_rejected(batch("b", 0, [change("n", 1, cid="ghost")]),
                             "unknown change course")

    def test_corrected_batch_reuses_batch_id(self):
        bad = batch("retry", 0, [change("n", 1, mark=101)])
        with self.assertRaisesRegex(ValueError, "exceeds 100"):
            self.ingest(bad)
        good = batch("retry", 0, [change("n", 1, mark=99)])
        receipt = self.ingest(good)
        self.assertEqual(receipt["seq"], 1)
        self.assertEqual(self.ingest(good), receipt)  # 精确重放

    def test_expected_seq_mismatch_is_rejected_without_side_effects(self):
        with self.assertRaisesRegex(ValueError, "expected_seq"):
            self.ingest(batch("b", 5, [change("n", 1)]))
        self.assertEqual(self.audit()["known_seq"], 0)
        receipt = self.ingest(batch("b", 0, [change("n", 1)]))
        self.assertEqual(receipt["seq"], 1)


class FactAndIdentityTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_same_fact_redelivered_by_another_batch_is_allowed(self):
        first = self.ingest(batch("b1", 0, [change("n", 1, mark=70)]))
        second = self.ingest(batch("b2", 1, [change("n", 1, mark=70)]))
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(second["changed"], [])  # 事实重复不列入 changed

    def test_conflicting_content_rejected_even_when_superseded(self):
        self.ingest(batch("b1", 0, [change("n", 5, mark=70),
                                    change("n", 1, mark=99)]))
        # revision 1 已被更高版本压过，但以不同内容再投递仍整批拒绝。
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.ingest(batch("b2", 1, [change("n", 1, mark=70)]))
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.ingest(batch("b3", 1, [change("n", 1, op="withdraw")]))
        # 完全相同的事实换个 batch_id 再投递则允许。
        receipt = self.ingest(batch("b4", 1, [change("n", 1, mark=99)]))
        self.assertEqual(receipt["seq"], 2)

    def test_identity_never_changes(self):
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b1", 0, [change("a", 1, sid="s2")]))
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b2", 0, [change("a", 1, cid="B")]))
        # 撤销之后身份仍然固定。
        self.ingest(batch("b3", 0, [change("a", 1, op="withdraw")]))
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b4", 1, [change("a", 2, sid="s2")]))
        # 过期修订也不许转学/转课。
        self.ingest(batch("b5", 1, [change("n", 5)]))
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b6", 2, [change("n", 3, sid="s2")]))
        # 同一批内对新 ID 的身份也必须一致。
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b7", 2, [change("m", 1, sid="s1"),
                                        change("m", 2, sid="s2")]))

    def test_first_seen_withdraw_establishes_identity_and_can_be_restored(self):
        receipt = self.ingest(batch("b1", 0, [change("w", 2, op="withdraw")]))
        self.assertEqual(receipt["changed"], [
            {"attempt_id": "w", "before_revision": None,
             "after_revision": 2, "withdrawn": True}])
        versions = {v["attempt_id"]: v for v in self.audit()["versions"]}
        self.assertEqual(versions["w"],
                         {"attempt_id": "w", "revision": 2, "withdrawn": True})
        self.ingest(batch("b2", 1, [change("w", 3, date="2026-01-05")]))
        versions = {v["attempt_id"]: v for v in self.audit()["versions"]}
        self.assertFalse(versions["w"]["withdrawn"])
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            self.ingest(batch("b3", 2, [change("w", 4, cid="B")]))

    def test_reordered_changes_and_key_order_are_the_same_batch(self):
        changes = [change("n1", 1, mark=71, date="2026-02-21"),
                   change("n2", 1, mark=72, date="2026-02-22")]
        receipt = self.ingest(batch("b1", 0, changes))
        reordered = {"changes": [dict(reversed(list(c.items())))
                                 for c in reversed(changes)],
                     "expected_seq": 0, "batch_id": "b1"}
        self.assertEqual(self.ingest(reordered), receipt)
        self.assertEqual(self.audit()["known_seq"], 1)  # 重放不分配新 seq

    def test_attempt_id_limit(self):
        students = [{"id": f"s{i:04d}"} for i in range(1999)]
        data = walkthrough_input()
        data["students"] = students
        data["attempts"] = [
            {"id": f"x{i:05d}", "student_id": students[i // 10]["id"],
             "course_id": "A", "date": f"2026-01-{i % 10 + 1:02d}", "mark": 80}
            for i in range(19990)]
        store = str(Path(self.tmp.name) / "limit.db")
        ledger.initialize(store, data)
        ten = [change(f"y{i}", 1, sid=f"s{i:04d}", date="2026-02-01")
               for i in range(10)]
        receipt = ledger.ingest(store, batch("fill", 0, ten))
        self.assertEqual(receipt["seq"], 1)  # 恰好 20000 个已知 ID
        with self.assertRaisesRegex(ValueError, "20000"):
            ledger.ingest(store, batch("overflow", 1,
                                       [change("z", 1, sid="s0000",
                                               date="2026-03-01")]))
        # 不新增 ID 的批次仍然可以提交。
        receipt = ledger.ingest(
            store, batch("ok", 1, [change("y0", 2, sid="s0000",
                                          date="2026-02-01")]))
        self.assertEqual(receipt["seq"], 2)


class ProjectionAtomicityTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        data = walkthrough_input()
        data["attempts"] = [
            {"id": "a", "student_id": "s1", "course_id": "A",
             "date": "2026-01-10", "mark": 80},
            {"id": "a2", "student_id": "s1", "course_id": "A",
             "date": "2026-01-20", "mark": 80},
        ]
        self.init(data)

    def test_atomic_swap_of_dates_in_one_batch(self):
        # 只把 a 改到 a2 的日期：整批失败，seq 与原日期都保持。
        with self.assertRaisesRegex(ValueError, "student/course/day"):
            self.ingest(batch("bad", 0, [change("a", 1, date="2026-01-20")]))
        self.assertEqual(self.audit()["known_seq"], 0)
        # 同一批中交换两者日期，最终不重复：成功。
        receipt = self.ingest(batch("swap", 0, [
            change("a", 1, date="2026-01-20"),
            change("a2", 1, date="2026-01-10")]))
        self.assertEqual(receipt["seq"], 1)
        self.assertEqual({c["attempt_id"] for c in receipt["changed"]},
                         {"a", "a2"})

    def test_mixed_legal_and_illegal_items_reject_whole_batch(self):
        with self.assertRaisesRegex(ValueError, "student/course/day"):
            self.ingest(batch("mix", 0, [
                change("new-ok", 1, cid="B", date="2026-03-01"),
                change("a", 1, date="2026-01-20")]))  # 与 a2 撞期
        result = self.audit()
        self.assertEqual(result["known_seq"], 0)
        self.assertNotIn("new-ok", {v["attempt_id"] for v in result["versions"]})

    def test_every_item_validated_even_stale_future_or_losing(self):
        # 落后修订、未来日期、最终未胜出的记录都独立校验。
        self.ingest(batch("b1", 0, [change("n", 5, mark=70)]))
        with self.assertRaisesRegex(ValueError, "exceeds 100"):
            self.ingest(batch("b2", 1, [change("n", 2, mark=150)]))  # 落后也校验
        with self.assertRaisesRegex(ValueError, "exceeds 100"):
            self.ingest(batch("b3", 1, [change("f", 1, date="2030-01-01",
                                               mark=150)]))  # 未来也校验
        with self.assertRaisesRegex(ValueError, "unknown change student"):
            self.ingest(batch("b4", 1, [change("n", 2, sid="ghost")]))
        self.assertEqual(self.audit()["known_seq"], 1)

    def test_superseded_versions_do_not_conflict_but_history_is_kept(self):
        self.ingest(batch("b1", 0, [change("a", 1, date="2026-01-15")]))
        # 旧版本让出的日期可以被新考试使用。
        self.ingest(batch("b2", 1, [change("c", 1, date="2026-01-10")]))
        # 历史回放仍能看到当时的投影。
        day0 = self.audit(known_seq=0)["audit"]["students"][0]["attempt_audit"]
        self.assertEqual({row["attempt_id"] for row in day0}, {"a", "a2"})
        day1 = self.audit(known_seq=1)["audit"]["students"][0]["attempt_audit"]
        self.assertEqual({row["attempt_id"] for row in day1}, {"a", "a2"})


class ReceiptTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_empty_batch_gets_seq_and_persistent_receipt(self):
        receipt = self.ingest(batch("empty", 0, []))
        self.assertEqual(receipt, {"batch_id": "empty", "seq": 1,
                                   "previous_seq": 0, "changed": []})
        self.assertEqual(self.ingest(batch("empty", 0, [])), receipt)

    def test_changed_lists_only_rising_or_new_ids(self):
        self.ingest(batch("b1", 0, [change("n", 5, mark=70)]))
        receipt = self.ingest(batch("b2", 1, [change("n", 3, mark=75)]))
        self.assertEqual(receipt["changed"], [])  # 较低版本补到不列入
        # 高版本只改来源 revision、date/mark 未变，也要列入。
        receipt = self.ingest(batch("b3", 2, [change("n", 6, mark=75)]))
        self.assertEqual(receipt["changed"], [
            {"attempt_id": "n", "before_revision": 5,
             "after_revision": 6, "withdrawn": False}])

    def test_changed_sorted_by_attempt_id(self):
        receipt = self.ingest(batch("b1", 0, [
            change("z", 1, cid="B", date="2026-02-21"),
            change("m", 1, date="2026-02-22"),
            change("a", 9, mark=90, date="2026-02-23")]))
        self.assertEqual([c["attempt_id"] for c in receipt["changed"]],
                         ["a", "m", "z"])

    def test_receipt_survives_reopen_and_replay(self):
        receipt = self.ingest(batch("b1", 0, [change("n", 1)]))
        # 新连接（等价于新进程打开同一文件）重放仍得原回执。
        self.assertEqual(self.ingest(batch("b1", 0, [change("n", 1)])),
                         receipt)
        self.assertEqual(self.audit()["known_seq"], 1)


class AuditTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_known_seq_bounds_are_not_clamped(self):
        self.ingest(batch("b1", 0, []))
        with self.assertRaisesRegex(ValueError, "known_seq"):
            self.audit(known_seq=2)
        with self.assertRaisesRegex(ValueError, "known_seq"):
            self.audit(known_seq=-1)
        with self.assertRaisesRegex(ValueError, "known_seq"):
            self.audit(known_seq=True)
        with self.assertRaisesRegex(ValueError, "known_seq"):
            self.audit(known_seq="1")
        self.assertEqual(self.audit(known_seq=1)["known_seq"], 1)
        self.assertEqual(self.audit()["known_seq"], 1)

    def test_as_of_default_and_validation(self):
        self.assertEqual(self.audit()["as_of"], "2026-03-01")
        self.assertEqual(self.audit(as_of="2026-01-01")["as_of"], "2026-01-01")
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.audit(as_of="2026-3-1")
        with self.assertRaisesRegex(ValueError, "canonical"):
            self.audit(as_of="2026-02-30")

    def test_versions_cover_known_ids_and_exclude_unknown(self):
        self.ingest(batch("b1", 0, [change("w", 1, op="withdraw"),
                                    change("n", 1)]))
        result = self.audit(known_seq=0)
        self.assertEqual({v["attempt_id"] for v in result["versions"]},
                         {"a", "b"})
        result = self.audit()
        versions = {v["attempt_id"]: v for v in result["versions"]}
        self.assertEqual(set(versions), {"a", "b", "w", "n"})
        self.assertTrue(versions["w"]["withdrawn"])
        self.assertEqual([v["attempt_id"] for v in result["versions"]],
                         ["a", "b", "n", "w"])

    def test_future_attempts_still_counted_as_ignored(self):
        self.ingest(batch("b1", 0, [change("f", 1, date="2027-06-01")]))
        result = self.audit(known_seq=1, as_of="2026-03-01")
        student = result["audit"]["students"][0]
        self.assertEqual(student["ignored_future_attempts"], 1)
        self.assertIn("f", {v["attempt_id"] for v in result["versions"]})

    def test_backdated_pass_changes_today_not_earlier_seq(self):
        data = walkthrough_input()
        data["attempts"] = [
            {"id": "x", "student_id": "s1", "course_id": "A",
             "date": "2026-01-10", "mark": 50},
            {"id": "y", "student_id": "s1", "course_id": "B",
             "date": "2026-02-10", "mark": 80},
        ]
        store = str(Path(self.tmp.name) / "axes.db")
        ledger.initialize(store, data)
        ledger.ingest(store, batch("fix", 0, [
            change("x", 1, mark=80, date="2026-01-10")]))
        before = ledger.audit(store, known_seq=0, as_of="2026-03-01")
        after = ledger.audit(store, known_seq=1, as_of="2026-03-01")
        statuses0 = {row["attempt_id"]: row["status"]
                     for row in before["audit"]["students"][0]["attempt_audit"]}
        self.assertEqual(statuses0, {"x": "failed", "y": "blocked_prerequisite"})
        self.assertTrue(after["audit"]["students"][0]["graduation_eligible"])

    def test_audit_does_not_write_back(self):
        self.audit(known_seq=0)
        self.assertEqual(self.audit()["known_seq"], 0)
        receipt = self.ingest(batch("b1", 0, [change("n", 1)]))
        self.assertEqual(receipt["seq"], 1)


class VisibilityTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_visible_version_is_max_revision_not_last_arrived(self):
        self.ingest(batch("b1", 0, [change("a", 3, date="2026-01-15",
                                           mark=85)]))
        # 更旧的 revision 晚到：可见版本仍是 revision 3。
        self.ingest(batch("b2", 1, [change("a", 1, date="2026-01-20",
                                           mark=95)]))
        versions = {v["attempt_id"]: v for v in self.audit()["versions"]}
        self.assertEqual(versions["a"]["revision"], 3)
        selected = self.student(self.audit())["selected_passes"]
        self.assertEqual(selected[0]["mark"], 85)  # 不是晚到修订的 95
        # 历史序号各自保持当时的胜者。
        v1 = {v["attempt_id"]: v for v in self.audit(known_seq=1)["versions"]}
        self.assertEqual(v1["a"]["revision"], 3)
        v0 = {v["attempt_id"]: v for v in self.audit(known_seq=0)["versions"]}
        self.assertEqual(v0["a"]["revision"], 0)


class CascadeTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init(chain_input())

    def statuses(self, **kwargs):
        rows = self.audit(**kwargs)["audit"]["students"][0]["attempt_audit"]
        return {row["attempt_id"]: (row["status"],
                                    row["missing_prerequisites"])
                for row in rows}

    def test_withdraw_cascades_through_prerequisite_chain(self):
        self.assertTrue(self.student(self.audit())["graduation_eligible"])
        self.ingest(batch("b1", 0, [change("a", 1, op="withdraw")]))
        statuses = self.statuses()
        self.assertEqual(statuses["b"], ("blocked_prerequisite", ["A"]))
        self.assertEqual(statuses["c"], ("blocked_prerequisite", ["B"]))
        student = self.student(self.audit())
        self.assertEqual(student["credits"], 0)
        self.assertEqual(student["selected_passes"], [])

    def test_lowered_mark_cascades_and_restore_unlocks(self):
        self.ingest(batch("b1", 0, [change("a", 1, mark=40)]))
        statuses = self.statuses()
        self.assertEqual(statuses["a"][0], "failed")
        self.assertEqual(statuses["b"][0], "blocked_prerequisite")
        self.assertEqual(statuses["c"][0], "blocked_prerequisite")
        self.ingest(batch("b2", 1, [change("a", 2, mark=90,
                                           date="2026-01-05")]))
        self.assertTrue(self.student(self.audit())["graduation_eligible"])
        # 历史序号保持级联失效的结果。
        self.assertEqual(self.statuses(known_seq=1)["c"][0],
                         "blocked_prerequisite")


class V2LedgerTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.init(v2_input())

    def plan(self, **kwargs):
        return self.student(self.audit(**kwargs))["recognition_plan"]

    def test_correction_triggers_re_recognition(self):
        # seq=0：zz 认定 A 以解锁 C；aa 认定 B 增加学分，两者同选。
        self.assertEqual(self.plan()["option_ids"], ["zz"])
        # 补报本校 A 及格后，zz 不再必要，最优方案改为只选 aa。
        self.ingest(batch("b1", 0, [
            change("a1", 1, date="2026-01-10", mark=70)]))
        self.assertEqual(self.plan()["option_ids"], ["aa"])
        # 旧时点的认定方案不变。
        self.assertEqual(self.plan(known_seq=0)["option_ids"], ["zz"])
        # 撤销本校 A 及格后回到 zz。
        self.ingest(batch("b2", 1, [change("a1", 2, op="withdraw")]))
        self.assertEqual(self.plan()["option_ids"], ["zz"])

    def test_same_seq_different_as_of_selects_different_plan(self):
        early = self.plan(as_of="2026-01-15")
        self.assertEqual(early["option_ids"], [])  # 方案尚未生效
        self.assertEqual(self.plan()["option_ids"], ["zz"])

    def test_v2_report_shape_is_preserved(self):
        report = self.audit()["audit"]
        self.assertEqual(report["schema_version"], 2)
        student = report["students"][0]
        self.assertIn("recognition_plan", student)
        sources = {row["course_id"]: row["source"]
                   for row in student["selected_passes"]}
        self.assertEqual(sources["A"], "transfer")


class FixtureEquivalenceTests(LedgerTestCase):
    def test_seq0_audit_matches_direct_service_run(self):
        for name in ("input.json", "input_v2.json"):
            data = load(FIXTURES / name)
            store = str(Path(self.tmp.name) / f"{name}.db")
            ledger.initialize(store, data)
            result = ledger.audit(store)
            self.assertEqual(result["audit"], run(data))
            self.assertEqual(result["known_seq"], 0)
            self.assertEqual(result["as_of"], data["as_of"])


if __name__ == "__main__":
    unittest.main()
