"""credit-disclosure 的披露规则、精确可行性与验收例。

隐私判断只用本快照的登记人数表与所列公开事实。测试里另写一份独立的
暴力枚举作为参照（仅用于很小的表），验证算法从不枚举完整候选表也能给出
与真值模型一致的结论。
"""
import copy
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from registrar.disclosure import report
from registrar.service import run

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


# ---------------------------------------------------------------------------
# 合成输入
# ---------------------------------------------------------------------------

COURSES = [
    {"id": "PASS", "credits": 3, "pass_mark": 60, "category": "STEM",
     "prerequisites": []},
]


def audit_input(eligible, as_of="2026-06-01"):
    """``eligible`` 是 {student_id: bool}；为真的学生通过 PASS 课。"""
    attempts = []
    for sid, ok in eligible.items():
        if ok:
            attempts.append({"id": "att-" + sid, "student_id": sid,
                             "course_id": "PASS", "date": "2026-01-01", "mark": 90})
    return {
        "as_of": as_of,
        "courses": copy.deepcopy(COURSES),
        "students": [{"id": sid} for sid in eligible],
        "program": {"required_courses": ["PASS"],
                    "minimum_credits": {"STEM": 3}},
        "attempts": attempts,
    }


def with_disclosure(data, departments, cohorts, memberships, protected,
                    requests, prior=None):
    data = copy.deepcopy(data)
    data["disclosure"] = {
        "departments": departments,
        "cohorts": cohorts,
        "memberships": [{"student_id": sid, "department": dept, "cohort": cohort}
                        for sid, (dept, cohort) in memberships.items()],
        "protected_cells": [{"department": d, "cohort": c} for d, c in protected],
        "requests": [{"department": d, "cohort": c} for d, c in requests],
        "prior": ([] if prior is None else [
            {"department": d, "cohort": c, "min": lo, "max": hi}
            for d, c, lo, hi in prior]),
    }
    return data


def items_by_scope(result):
    return {(row["department"], row["cohort"]): row for row in result["items"]}


def brute_eligible_tables(caps):
    """独立参照：枚举 0..登记人数 的全部人数表。只用于极小规模。"""
    d, c = len(caps), len(caps[0])
    ranges = [range(caps[i][j] + 1) for i in range(d) for j in range(c)]
    for flat in itertools.product(*ranges):
        yield [list(flat[i * c:(i + 1) * c]) for i in range(d)]


def table_value(table, scope):
    i, j = scope
    if i is None and j is None:
        return sum(map(sum, table))
    if j is None:
        return sum(table[i])
    if i is None:
        return sum(table[k][j] for k in range(len(table)))
    return table[i][j]


# ---------------------------------------------------------------------------
# 两个可区分验收例
# ---------------------------------------------------------------------------

class DistinguishingExampleTests(unittest.TestCase):
    def build(self, first_eligible, second_eligible):
        """A 院系两届各 1 人、2 人，保护 1 人那格。"""
        eligible = {"first": first_eligible,
                    "second-a": second_eligible, "second-b": False}
        data = audit_input(eligible)
        return with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"first": ("A", "c1"), "second-a": ("A", "c2"),
             "second-b": ("A", "c2")},
            protected=[("A", "c1")],
            requests=[("A", None), ("A", "c2")])

    def test_total_one_then_second_cell_band(self):
        result = report(self.build(False, True))
        rows = items_by_scope(result)
        # 合计 1：只能发 [0,1]；随后的 c2=1 也仍可发 [0,1]。
        self.assertEqual((rows[("A", None)]["status"], rows[("A", None)]["bounds"]),
                         ("band", [0, 1]))
        self.assertEqual((rows[("A", "c2")]["status"], rows[("A", "c2")]["bounds"]),
                         ("band", [0, 1]))
        self.assertEqual(rows[("A", None)]["students"], 3)
        self.assertEqual(rows[("A", "c2")]["students"], 2)

    def test_total_two_forces_second_withheld(self):
        result = report(self.build(True, True))
        rows = items_by_scope(result)
        # 合计 2：发 [2,3]；此后 c2 若发 [0,1] 会让 c1 必须为 1，只能隐藏。
        self.assertEqual((rows[("A", None)]["status"], rows[("A", None)]["bounds"]),
                         ("band", [2, 3]))
        self.assertEqual((rows[("A", "c2")]["status"], rows[("A", "c2")]["bounds"]),
                         ("withheld", None))

    def test_report_shape(self):
        result = report(self.build(False, True))
        self.assertEqual(set(result), {"schema_version", "as_of", "enrollment", "items"})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["as_of"], "2026-06-01")
        self.assertEqual(result["enrollment"], [
            {"department": "A", "cohort": "c1", "students": 1},
            {"department": "A", "cohort": "c2", "students": 2}])


class TwoByTwoExampleTests(unittest.TestCase):
    """四格登记人数都为 1；已公开首行=1、首列=1；保护左上角；请求全校。"""

    def build(self, eligible):
        data = audit_input(eligible)
        return with_disclosure(
            data, ["D0", "D1"], ["C0", "C1"],
            {"a": ("D0", "C0"), "b": ("D0", "C1"),
             "c": ("D1", "C0"), "d": ("D1", "C1")},
            protected=[("D0", "C0")],
            requests=[(None, None)],
            prior=[("D0", None, 1, 1), (None, "C0", 1, 1)])

    def test_exact_two_is_withheld(self):
        # 首行=1、首列=1 迫使真实合计恒为 2（两解 a=1 或 a=0 都合计 2）。
        # 单发精确 2 时左上角仍可取 0/1，但精确 1/3 与 [0,1] 会暴露它，
        # 整档、整段都不可用；不能因为真实值落在安全组而泄露档位。
        result = report(self.build({"a": True, "b": False, "c": False, "d": True}))
        row = result["items"][0]
        self.assertEqual((row["status"], row["bounds"]), ("withheld", None))
        self.assertEqual(row["students"], 4)

    def test_other_true_value_withholds_too(self):
        # 另一个同样满足首行/首列=1 的快照（a=0），真实合计仍是 2；
        # 报告同样隐藏，不因内部真实解不同而泄露哪一组。
        result = report(self.build({"a": False, "b": True, "c": True, "d": False}))
        self.assertEqual((result["items"][0]["status"],
                          result["items"][0]["bounds"]), ("withheld", None))

    def test_matches_full_enumeration(self):
        caps = [[1, 1], [1, 1]]
        tables = [t for t in brute_eligible_tables(caps)
                  if table_value(t, (0, None)) == 1
                  and table_value(t, (None, 0)) == 1]
        exposed = {table_value(t, (0, 0)) for t in tables if table_value(t, (None, None)) == 2}
        # 参照确认：精确 2 下左上角仍可取 0/1，但整档因其它组不安全而被弃用。
        self.assertEqual(exposed, {0, 1})


# ---------------------------------------------------------------------------
# 已公开事实与真实人数
# ---------------------------------------------------------------------------

class PriorFactsTests(unittest.TestCase):
    def base(self, eligible, prior):
        roster = {"a": False, "b": False, "c": False, "d": False}
        roster.update(eligible)
        data = audit_input(roster)
        return with_disclosure(
            data, ["A", "B"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("B", "c1"),
             "c": ("A", "c2"), "d": ("B", "c2")},
            protected=[("A", "c1")],
            requests=[("B", "c2")],
            prior=prior)

    def test_prior_conflict_inferred_through_aggregate(self):
        # 真实合计 1，prior 却给出首列=1、首行=1（会迫使合计至少 2）。
        data = self.base({"d": True},
                         prior=[("A", None, 1, 1), (None, "c1", 1, 1)])
        with self.assertRaises(ValueError):
            report(data)

    def test_prior_beyond_registered_rejected(self):
        data = self.base({"a": True}, prior=[("B", "c2", 0, 2)])
        with self.assertRaisesRegex(ValueError, "exceeds the registered"):
            report(data)

    def test_prior_zero_range_is_exact(self):
        # 某个非受保护格子的 prior 是精确 0：它不暴露受保护格 A/c1。
        data = self.base({}, prior=[("B", "c2", 0, 0)])
        result = report(data)
        self.assertEqual(result["items"][0]["status"], "exact")
        self.assertEqual(result["items"][0]["bounds"], [0, 0])

    def test_prior_that_pins_protected_cell_rejected(self):
        # 直接给出受保护格精确值，无论请求什么都拒绝，且不输出真值。
        data = self.base({"a": True, "b": False, "c": False, "d": False},
                         prior=[("A", "c1", 1, 1)])
        with self.assertRaisesRegex(ValueError, "determine a protected cell"):
            report(data)

    def test_prior_indirectly_pins_protected_cell_rejected(self):
        # 合计与另一格一起把受保护格定死。
        data = self.base({"a": True, "b": False, "c": False, "d": False},
                         prior=[(None, "c1", 1, 1), ("B", "c1", 0, 0)])
        with self.assertRaisesRegex(ValueError, "determine a protected cell"):
            report(data)

    def test_infeasible_prior_rejected(self):
        data = self.base({"a": True}, prior=[("A", None, 2, 2), ("A", "c1", 0, 0)])
        with self.assertRaises(ValueError):
            report(data)


# ---------------------------------------------------------------------------
# 档位与分组
# ---------------------------------------------------------------------------

class TierTests(unittest.TestCase):
    def test_zero_student_scopes_publish_exact_zero(self):
        data = audit_input({"s": True})
        data = with_disclosure(
            data, ["A", "B"], ["c1", "c2"],
            {"s": ("A", "c1")}, protected=[], requests=[("B", "c1"), (None, "c2")])
        rows = items_by_scope(report(data))
        self.assertEqual((rows[("B", "c1")]["status"], rows[("B", "c1")]["bounds"]),
                         ("exact", [0, 0]))
        self.assertEqual((rows[(None, "c2")]["status"], rows[(None, "c2")]["bounds"]),
                         ("exact", [0, 0]))

    def test_safe_exact_aggregate_published(self):
        # 没有受保护格时精确汇总总能发布。
        data = audit_input({"a": True, "b": False})
        data = with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("A", "c2")},
            protected=[], requests=[("A", None), (None, None)])
        rows = items_by_scope(report(data))
        self.assertEqual((rows[("A", None)]["status"], rows[("A", None)]["bounds"]),
                         ("exact", [1, 1]))
        self.assertEqual((rows[(None, None)]["status"], rows[(None, None)]["bounds"]),
                         ("exact", [1, 1]))

    def test_band_truncated_by_registration(self):
        # 登记 5 人，片区段 [0,1],[2,3],[4,5]；真实 4 → 末段 [4,5]。
        data = audit_input({"a": True, "b": True, "c": True, "d": True, "e": False})
        data = with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("A", "c1"), "c": ("A", "c1"),
             "d": ("A", "c2"), "e": ("A", "c2")},
            protected=[("A", "c2")], requests=[("A", None)])
        row = report(data)["items"][0]
        # 精确档的极端组（合计 0 或 5）会暴露 c2，弃用；区间档整档安全，
        # 真实合计 4 落在末段 [4,5]（末段按登记 5 人截断）。
        self.assertEqual(row["students"], 5)
        self.assertEqual((row["status"], row["bounds"]), ("band", [4, 5]))

    def test_band_group_boundaries_are_fixed_not_narrowed(self):
        # 登记 [3,3]：合计 0..6。区间档的固定段 [4,5] 让 c2 仍有 1..3；
        # 末段 [6,6] 会迫使 c2=3 而暴露它，故整档弃用而隐藏。若实现按当前
        # 可行值范围把末段缩窄，结论就会不同——这里确认它用固定边界。
        data = audit_input({f"s{n}": n < 4 for n in range(6)})
        data = with_disclosure(
            data, ["A"], ["c1", "c2"],
            {f"s{n}": ("A", "c1" if n < 3 else "c2") for n in range(6)},
            protected=[("A", "c2")], requests=[("A", None)])
        row = report(data)["items"][0]
        self.assertEqual(row["status"], "withheld")

    def test_empty_band_segments_are_skipped_not_unsafe(self):
        # prior 把合计锁在 0..1：[2,3]、[4,5] 是非空但当前不可达的空组，
        # 不得因此判整档不安全；真实 1 应正常发 [0,1]。
        data = audit_input({"a": True, "b": False, "c": False, "d": False})
        data = with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("A", "c1"),
             "c": ("A", "c2"), "d": ("A", "c2")},
            protected=[("A", "c1")], requests=[("A", None)],
            prior=[("A", None, 0, 1)])
        row = report(data)["items"][0]
        self.assertEqual((row["status"], row["bounds"]), ("band", [0, 1]))

    def test_band_publishes_when_all_fixed_segments_safe(self):
        # 登记 [3,2]：合计 0..5。精确档的极端组会暴露 c2，弃用；区间档每段
        # 都让 c2 仍有两个取值，安全，取真实合计 4 所在的固定段 [4,5]。
        data = audit_input({f"s{n}": n < 4 for n in range(5)})
        data = with_disclosure(
            data, ["A"], ["c1", "c2"],
            {f"s{n}": ("A", "c1" if n < 3 else "c2") for n in range(5)},
            protected=[("A", "c2")], requests=[("A", None)])
        row = report(data)["items"][0]
        self.assertEqual((row["status"], row["bounds"]), ("band", [4, 5]))


# ---------------------------------------------------------------------------
# v2：转学分能否及时解锁先修改变真实发布数字
# ---------------------------------------------------------------------------

class TransferTimingTests(unittest.TestCase):
    def build(self, effective_date):
        data = {
            "as_of": "2026-06-01",
            "courses": [
                {"id": "A", "credits": 3, "pass_mark": 60,
                 "category": "STEM", "prerequisites": []},
                {"id": "B", "credits": 4, "pass_mark": 60,
                 "category": "STEM", "prerequisites": ["A"]}],
            "students": [{"id": "s-a"}, {"id": "s-b"}],
            "program": {"required_courses": ["A", "B"],
                        "minimum_credits": {"STEM": 7}},
            "attempts": [
                {"id": "b-a", "student_id": "s-a", "course_id": "B",
                 "date": "2026-03-01", "mark": 90},
                {"id": "b-b", "student_id": "s-b", "course_id": "B",
                 "date": "2026-03-01", "mark": 90}],
            "transfer": {
                "credit_limit": 3,
                "evidence": [{"id": "e-a", "student_id": "s-a",
                              "date": "2026-01-01"}],
                "options": [{"id": "opt-a", "student_id": "s-a",
                             "evidence_ids": ["e-a"], "course_ids": ["A"],
                             "effective_date": effective_date}]},
        }
        return with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"s-a": ("A", "c1"), "s-b": ("A", "c2")},
            protected=[("A", "c2")], requests=[("A", "c1"), (None, None)])

    def test_in_time_transfer_unlocks_and_counts(self):
        report_v2 = run(self.build("2026-01-10"))
        self.assertEqual(report_v2["schema_version"], 2)
        rows = {r["student_id"]: r for r in report_v2["students"]}
        self.assertTrue(rows["s-a"]["graduation_eligible"])
        self.assertEqual(rows["s-a"]["recognition_plan"]["option_ids"], ["opt-a"])
        result = report(self.build("2026-01-10"))
        by = items_by_scope(result)
        self.assertEqual(by[("A", "c1")]["bounds"], [1, 1])  # 不暴露 c2 的 0

    def test_late_transfer_blocks_and_value_changes(self):
        # 认定晚于 B 考试：B 仍被先修阻断，s-a 不毕业，c1 的真实人数为 0。
        report_v2 = run(self.build("2026-04-01"))
        rows = {r["student_id"]: r for r in report_v2["students"]}
        self.assertFalse(rows["s-a"]["graduation_eligible"])
        result = report(self.build("2026-04-01"))
        by = items_by_scope(result)
        self.assertEqual(by[("A", "c1")]["bounds"], [0, 0])

    def test_counts_follow_graduation_not_exam_pass(self):
        # 两名学生 B 考试都及格，但都没有先修 A 的合法凭据 → 都不毕业。
        # 全校合计因此是 0；受保护的 c2 此时仍无毕业者，0 与 1 不可区分。
        data = self.build("2026-04-01")
        data["transfer"]["options"] = []
        baseline = run(data)
        self.assertEqual(baseline["summary"]["graduation_eligible"], 0)
        result = report(data)
        rows = items_by_scope(result)
        self.assertEqual(rows[(None, None)]["bounds"], [0, 1])
        self.assertEqual(rows[("A", "c1")]["bounds"], [0, 0])


# ---------------------------------------------------------------------------
# 输入校验、确定性与计算量
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def build(self):
        data = audit_input({"a": True, "b": False})
        return with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("A", "c2")},
            protected=[("A", "c1")], requests=[("A", None)])

    def mutate(self, path, value):
        data = self.build()
        node = data
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value
        return data

    def test_requires_disclosure_object(self):
        data = self.build()
        del data["disclosure"]
        with self.assertRaisesRegex(ValueError, "disclosure is required"):
            report(data)

    def test_unknown_membership_reference(self):
        data = self.build()
        data["disclosure"]["memberships"][0]["student_id"] = "ghost"
        with self.assertRaisesRegex(ValueError, "unknown membership student"):
            report(data)

    def test_membership_must_cover_each_student_once(self):
        data = self.build()
        data["disclosure"]["memberships"].pop()
        with self.assertRaisesRegex(ValueError, "exactly one membership"):
            report(data)
        data = self.build()
        data["disclosure"]["memberships"].append(
            {"student_id": "a", "department": "A", "cohort": "c2"})
        with self.assertRaisesRegex(ValueError, "duplicate membership"):
            report(data)

    def test_boolean_bounds_rejected(self):
        data = self.build()
        data["disclosure"]["prior"] = [
            {"department": "A", "cohort": "c2", "min": True, "max": 1}]
        with self.assertRaisesRegex(ValueError, "integer"):
            report(data)

    def test_bad_lists_rejected(self):
        cases = [
            (("disclosure", "departments"), []),
            (("disclosure", "departments"), ["A"] * 9),
            (("disclosure", "cohorts"), ["c1", "c1"]),
            (("disclosure", "protected_cells"),
             [{"department": "A", "cohort": None}]),
            (("disclosure", "requests"),
             [{"department": None, "cohort": None}] * 2),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    report(self.mutate(path, value))

    def test_protected_zero_cell_rejected(self):
        data = audit_input({"a": True, "b": False})
        data = with_disclosure(
            data, ["A", "B"], ["c1"],
            {"a": ("A", "c1"), "b": ("A", "c1")},
            protected=[("B", "c1")], requests=[])
        with self.assertRaisesRegex(ValueError, "registered students"):
            report(data)

    def test_reordering_new_arrays_is_irrelevant(self):
        data = self.build()
        shuffled = copy.deepcopy(data)
        node = shuffled["disclosure"]
        for field in ("memberships", "protected_cells", "requests", "prior"):
            node[field] = list(reversed(node[field]))
        node["departments"] = list(reversed(node["departments"]))
        node["cohorts"] = list(reversed(node["cohorts"]))
        # 院系/入学届改名顺序会改变下标但不改变报告名字内容；这里保持同集合。
        ordered = json.dumps(report(data), ensure_ascii=False, sort_keys=True)
        again = json.dumps(report(shuffled), ensure_ascii=False, sort_keys=True)
        self.assertEqual(ordered, again)

    def test_request_order_is_preserved(self):
        data = self.build()
        data["disclosure"]["requests"] = [
            {"department": None, "cohort": None},
            {"department": "A", "cohort": "c2"}]
        result = report(data)
        self.assertEqual([(r["department"], r["cohort"]) for r in result["items"]],
                         [(None, None), ("A", "c2")])

    def test_empty_requests_allowed(self):
        data = self.build()
        data["disclosure"]["requests"] = []
        self.assertEqual(report(data)["items"], [])


class ExhaustiveReferenceTests(unittest.TestCase):
    """在极小表上，用完整枚举的独立参照核对发布结论。

    只枚举单格 0/1 的 2×2 表（至多 16 张），参照模型与实现互不共享代码；
    规模用例另有测试说明真实判断不会枚举完整候选表。
    """

    def build(self, eligible, protected, request):
        return with_disclosure(
            audit_input(eligible), ["D0", "D1"], ["C0", "C1"],
            {"a": ("D0", "C0"), "b": ("D0", "C1"),
             "c": ("D1", "C0"), "d": ("D1", "C1")},
            protected=protected, requests=[request], prior=[])

    @staticmethod
    def index(name, options):
        return None if name is None else options.index(name)

    def decide(self, tables, protected_scopes, scope, real, cap):
        """独立参照：完整枚举后按精确、区间、隐藏的顺序决定。

        ``cap`` 是该口径的公开登记人数；区间档的末段按它截断，其余段固定。
        """

        def safe(subset):
            return all(len({table_value(t, s) for t in subset}) >= 2
                       for s in protected_scopes)

        for tier in ("exact", "band"):
            groups = ([(v, v) for v in sorted({table_value(t, scope) for t in tables})]
                      if tier == "exact" else
                      [(s, min(s + 1, cap)) for s in range(0, cap + 1, 2)])
            chosen = None
            usable = True
            for lo, hi in groups:
                subset = [t for t in tables if lo <= table_value(t, scope) <= hi]
                if not subset:
                    continue
                if not safe(subset):
                    usable = False
                    break
                if lo <= real <= hi:
                    chosen = (lo, hi)
            if usable and chosen is not None:
                return tier, list(chosen)
        return "withheld", None

    def test_band_tier_against_full_enumeration(self):
        # 单院系两届登记 3、2 人，保护第二格；枚举全部 12 张表核对区间档。
        caps = [[3, 2]]
        table_names = {(0, 0): "x", (0, 1): "y"}
        seen = set()
        for true_table in brute_eligible_tables(caps):
            x, y = true_table[0]
            data = audit_input(
                {f"x{k}": k < x for k in range(3)} | {f"y{k}": k < y for k in range(2)})
            data = with_disclosure(
                data, ["D"], ["C0", "C1"],
                {**{f"x{k}": ("D", "C0") for k in range(3)},
                 **{f"y{k}": ("D", "C1") for k in range(2)}},
                protected=[("D", "C1")], requests=[("D", None)])
            row = report(data)["items"][0]
            tables = list(brute_eligible_tables(caps))
            expected = self.decide(tables, [(0, 1)], (0, None), x + y, 5)
            self.assertEqual((row["status"], row["bounds"]), expected)
            seen.add(row["status"])
        self.assertIn("band", seen)

        caps = [[1, 1], [1, 1]]
        names = ["a", "b", "c", "d"]
        table_names = {(0, 0): "a", (0, 1): "b", (1, 0): "c", (1, 1): "d"}
        for true_table in brute_eligible_tables(caps):
            eligible = {table_names[(i, j)]: true_table[i][j] == 1
                        for i in range(2) for j in range(2)}
            for protected in ([("D0", "C0")], []):
                protected_scopes = [(0, 0)] if protected else []
                for request in (("D1", "C1"), (None, None), ("D0", None)):
                    with self.subTest(eligible=eligible, protected=protected,
                                      request=request):
                        d, c = request
                        scope = (self.index(d, ["D0", "D1"]),
                                 self.index(c, ["C0", "C1"]))
                        real = table_value(true_table, scope)
                        cap = (4 if scope == (None, None)
                               else 2 if None in scope else 1)
                        expected = self.decide(
                            list(brute_eligible_tables(caps)),
                            protected_scopes, scope, real, cap)
                        row = report(self.build(eligible, protected, request))["items"][0]
                        self.assertEqual((row["status"], row["bounds"]), expected)


class DifferentialTests(unittest.TestCase):
    """固定种子下，把整条发布流水线与完整枚举参照逐例比对。"""

    @staticmethod
    def scope_cap(caps, scope):
        d, c = len(caps), len(caps[0])
        if scope == (None, None):
            return sum(map(sum, caps))
        if scope[1] is None:
            return sum(caps[scope[0]])
        if scope[0] is None:
            return sum(caps[i][scope[1]] for i in range(d))
        return caps[scope[0]][scope[1]]

    def run_seed(self, seed, trials):
        random.seed(seed)
        for _ in range(trials):
            d, c = random.randint(1, 2), random.randint(1, 2)
            caps = [[random.randint(0, 2) for _ in range(c)] for _ in range(d)]
            if not any(map(any, caps)):
                continue
            true_table = [[random.randint(0, caps[i][j]) for j in range(c)]
                          for i in range(d)]
            eligible, memberships, sid = {}, {}, 0
            for i in range(d):
                for j in range(c):
                    for k in range(caps[i][j]):
                        memberships[f"s{sid}"] = (f"D{i}", f"C{j}")
                        eligible[f"s{sid}"] = k < true_table[i][j]
                        sid += 1
            scopes = ([(i, j) for i in range(d) for j in range(c)]
                      + [(i, None) for i in range(d)]
                      + [(None, j) for j in range(c)] + [(None, None)])
            prior, used = [], set()
            for _ in range(random.randint(0, 2)):
                scope = random.choice(scopes)
                if scope in used:
                    continue
                used.add(scope)
                value = table_value(true_table, scope)
                cap = self.scope_cap(caps, scope)
                prior.append((scope, (random.randint(0, value),
                                      random.randint(value, cap))))
            pop = [(i, j) for i in range(d) for j in range(c) if caps[i][j] > 0]
            protected = []
            for _ in range(random.randint(0, min(2, len(pop)))):
                choice = random.choice(pop)
                if choice not in protected:
                    protected.append(choice)
            requests = random.sample(scopes, random.randint(0, min(4, len(scopes))))
            name = lambda index, kind: None if index is None else f"{kind}{index}"
            data = with_disclosure(
                audit_input(eligible),
                [f"D{i}" for i in range(d)], [f"C{j}" for j in range(c)],
                memberships, [(f"D{i}", f"C{j}") for i, j in protected],
                [(name(i, "D"), name(j, "C")) for i, j in requests],
                [(name(i, "D"), name(j, "C"), lo, hi)
                 for (i, j), (lo, hi) in prior])
            tables = list(brute_eligible_tables(caps))
            facts = dict(prior)
            feasible = [t for t in tables
                        if all(lo <= table_value(t, s) <= hi
                               for s, (lo, hi) in facts.items())]

            def two_values(rows):
                return all(len({table_value(t, s) for t in rows}) >= 2
                           for s in protected)

            reject = (any(not (lo <= table_value(true_table, s) <= hi)
                          for s, (lo, hi) in facts.items())
                      or not feasible or not two_values(feasible))
            with self.subTest(caps=caps, true_table=true_table, prior=prior,
                              protected=protected, requests=requests):
                if reject:
                    with self.assertRaises(ValueError):
                        report(data)
                    continue
                expected = []
                for scope in requests:
                    real = table_value(true_table, scope)
                    chosen, status = None, "withheld"
                    for tier in ("exact", "band"):
                        cap = self.scope_cap(caps, scope)
                        groups = (
                            [(v, v) for v in sorted(
                                {table_value(t, scope) for t in feasible})]
                            if tier == "exact" else
                            [(s, min(s + 1, cap)) for s in range(0, cap + 1, 2)])
                        pick, usable = None, True
                        for lo, hi in groups:
                            subset = [t for t in feasible
                                      if lo <= table_value(t, scope) <= hi]
                            if not subset:
                                continue
                            if not two_values(subset):
                                usable = False
                                break
                            if lo <= real <= hi:
                                pick = (lo, hi)
                        if usable and pick is not None:
                            chosen, status = pick, tier
                            break
                    if chosen is not None:
                        facts = dict(facts)
                        facts[scope] = chosen
                        feasible = [t for t in feasible
                                    if chosen[0] <= table_value(t, scope) <= chosen[1]]
                        expected.append((scope[0], scope[1], status, list(chosen)))
                    else:
                        expected.append((scope[0], scope[1], "withheld", None))
                actual = [
                    (None if row["department"] is None
                     else int(row["department"][1:]),
                     None if row["cohort"] is None else int(row["cohort"][1:]),
                     row["status"], row["bounds"])
                    for row in report(data)["items"]]
                self.assertEqual(actual, expected)

    def test_random_snapshots_match_full_enumeration(self):
        self.run_seed(99, 250)


class ScaleTests(unittest.TestCase):
    """规模用例：算法工作量与完整候选表数量无关。"""

    def big(self):
        eligible = {}
        memberships = {}
        depts = [f"D{k}" for k in range(8)]
        cohorts = [f"Y{k}" for k in range(8)]
        index = 0
        for di, dept in enumerate(depts):
            for ci, cohort in enumerate(cohorts):
                for n in range(31):  # 64 格 × 31 ≈ 1984 名学生
                    sid = f"s{index}"
                    memberships[sid] = (dept, cohort)
                    eligible[sid] = (index % 2 == 0)
                    index += 1
        data = audit_input(eligible)
        return with_disclosure(
            data, depts, cohorts, memberships,
            protected=[(depts[k], cohorts[k]) for k in range(8)],
            requests=[(None, None), (depts[0], None), (None, cohorts[0]),
                      (depts[0], cohorts[0])])

    def test_large_snapshot_completes_quickly(self):
        data = self.big()
        start = time.monotonic()
        result = report(data)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 30.0)
        self.assertEqual(len(result["enrollment"]), 64)
        self.assertEqual(sum(row["students"] for row in result["enrollment"]), 1984)
        self.assertEqual(len(result["items"]), 4)
        # 隐私与正确性：每个受保护格子仍有至少两个可能值（因精确/区间要么
        # 安全要么隐藏，这一点由逐条请求后的约束保证）；这里只确认有输出。
        for row in result["items"]:
            self.assertIn(row["status"], {"exact", "band", "withheld"})
            if row["status"] == "withheld":
                self.assertIsNone(row["bounds"])

    def test_work_is_independent_of_table_count(self):
        # 登记人数表有 Π(每格+1) 张候选，规模用例约 32^64 ≈ 10^96 张；
        # 若逐张遍历在物理上不可能。算法只做精确环流，故报告在秒级完成，
        # 且把一门「格子里再拆细」到每格 2000 人仍不改变结点数。
        data = self.big()
        start = time.monotonic()
        report(data)
        self.assertLess(time.monotonic() - start, 10.0)

    def test_single_huge_cell_scanned_per_candidate(self):
        # 一个受保护格之外有 2000 人：精确档最多逐个候选值判断 2001 次，
        # 每次一次小环流，因此也远低于枚举整张表的成本。
        eligible = {f"p{n}": n < 1 for n in range(2000)}
        data = audit_input(eligible)
        data = with_disclosure(
            data, ["A"], ["c1"], {f"p{n}": ("A", "c1") for n in range(2000)},
            protected=[("A", "c1")], requests=[("A", "c1")])
        start = time.monotonic()
        row = report(data)["items"][0]
        self.assertLess(time.monotonic() - start, 10.0)
        self.assertEqual(row["students"], 2000)
        self.assertIn(row["status"], {"band", "withheld"})


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def invoke(source, output):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "registrar.disclosure_cli",
         "--source", str(source), "--output", str(output)],
        cwd=REPO, env=env, capture_output=True, text=True)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, value):
        path = self.dir / "in.json"
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def sample(self):
        data = audit_input({"a": True, "b": False, "c": False})
        return with_disclosure(
            data, ["A"], ["c1", "c2"],
            {"a": ("A", "c1"), "b": ("A", "c2"), "c": ("A", "c2")},
            protected=[("A", "c1")], requests=[("A", None), ("A", "c2")])

    def test_console_script_is_declared(self):
        pyproject = (REPO / "pyproject.toml").read_text()
        self.assertIn(
            'credit-disclosure = "registrar.disclosure_cli:main"', pyproject)

    def test_report_written_atomically(self):
        source = self.write(self.sample())
        output = self.dir / "nested" / "report.json"
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report_data = json.loads(output.read_text())
        self.assertEqual(report_data["schema_version"], 1)
        self.assertEqual(set(report_data),
                         {"schema_version", "as_of", "enrollment", "items"})
        self.assertIn("'withheld'", proc.stdout)

    def test_invalid_input_exits_2_and_creates_nothing(self):
        bad = self.sample()
        bad["disclosure"]["protected_cells"] = [{"department": "X", "cohort": "c1"}]
        source = self.write(bad)
        output = self.dir / "out.json"
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("credit-disclosure", proc.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_never_overwritten(self):
        source = self.write(self.sample())
        output = self.dir / "out.json"
        output.write_text("KEEP ME")
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")

    def test_reordered_input_yields_identical_report(self):
        data = self.sample()
        shuffled = copy.deepcopy(data)
        node = shuffled["disclosure"]
        node["memberships"] = list(reversed(node["memberships"]))
        out1 = self.dir / "a.json"
        out2 = self.dir / "b.json"
        p1 = invoke(self.write(data), out1)
        p2 = invoke(self.write(shuffled), out2)
        self.assertEqual((p1.returncode, p2.returncode), (0, 0))
        self.assertEqual(json.loads(out1.read_text()), json.loads(out2.read_text()))

    def test_fixture_v2_runs(self):
        output = self.dir / "fixture.json"
        proc = invoke(REPO / "fixtures" / "input_disclosure.json", output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(output.read_text())
        self.assertEqual(len(data["enrollment"]), 4)
        # 与 docs/disclosure.md 的合成示例表一致，防止文档漂移。
        self.assertEqual(
            [(row["department"], row["cohort"], row["status"], row["bounds"])
             for row in data["items"]],
            [("computing", None, "band", [2, 3]),
             ("computing", "2023", "band", [2, 3]),
             ("design", None, "exact", [2, 2]),
             (None, None, "band", [4, 5]),
             (None, "2024", "withheld", None),
             ("computing", "2024", "band", [0, 1])])
        # 受保护格 computing/2024 的真实人数 0 不得出现在借它计算的那些条目里
        # 冒充精确结论；其自身也只发布 [0,1]。
        protected = [row for row in data["items"]
                     if (row["department"], row["cohort"]) == ("computing", "2024")]
        self.assertEqual(protected[0]["status"], "band")

    def test_min_fixture_matches_acceptance(self):
        output = self.dir / "min.json"
        proc = invoke(REPO / "fixtures" / "input_disclosure_min.json", output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(output.read_text())
        self.assertEqual(
            [(row["status"], row["bounds"]) for row in data["items"]],
            [("band", [2, 3]), ("withheld", None)])


if __name__ == "__main__":
    unittest.main()
