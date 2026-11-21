import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from registrar.awards import review
from registrar.service import run

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def stem_course(cid, credits=3, prerequisites=None):
    return {"id": cid, "credits": credits, "pass_mark": 60,
            "category": "STEM", "prerequisites": list(prerequisites or [])}


def v1_input(student_ids=("s1",), courses=("A", "B", "C", "D"),
             passed=("A", "B", "C", "D"), required=("B",),
             minimum_credits=None, groups=None, marks=None):
    data = {
        "as_of": "2026-04-01",
        "courses": [stem_course(cid) for cid in courses],
        "students": [{"id": sid} for sid in student_ids],
        "program": {
            "required_courses": list(required),
            "minimum_credits": minimum_credits or {"STEM": 6},
        },
        "attempts": [
            {"id": f"{cid}-{sid}", "student_id": sid, "course_id": cid,
             "date": "2026-02-01",
             "mark": (marks or {}).get((sid, cid), 80)}
            for sid in student_ids for cid in passed
        ],
    }
    data["awards"] = {
        "student_ids": list(student_ids),
        "groups": groups if groups is not None else [
            {"id": "practice", "course_ids": ["C", "D"],
             "minimum_credits": 3, "source": "any"},
            {"id": "project", "course_ids": ["C"],
             "minimum_credits": 3, "source": "any"},
        ],
    }
    return data


def groups_practice_project(source_practice="any", source_project="any"):
    return [
        {"id": "practice", "course_ids": ["C", "D"],
         "minimum_credits": 3, "source": source_practice},
        {"id": "project", "course_ids": ["C"],
         "minimum_credits": 3, "source": source_project},
    ]


def by_id(rows):
    return {row["course_id"]: row for row in rows}


class GreedyDiscriminationTests(unittest.TestCase):
    """契约规定的反逐组贪心案例：B/A/C/D 均 3 学分 STEM，B 主修必修。"""

    def test_full_passes_assign_d_to_practice_and_c_to_project(self):
        data = v1_input()
        report = review(data)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["as_of"], "2026-04-01")
        row = report["students"][0]
        self.assertEqual(row["student_id"], "s1")
        self.assertTrue(row["primary"]["eligible"])
        self.assertEqual(row["primary"]["course_ids"], ["A", "B"])
        self.assertEqual(row["primary"]["credits"], 6)
        self.assertEqual(row["primary"]["missing_required"], [])
        self.assertEqual(row["primary"]["credit_deficits"], {"STEM": 0})
        secondary = row["secondary"]
        self.assertTrue(secondary["eligible"])
        self.assertEqual(secondary["credits"], 6)
        self.assertEqual(secondary["groups"], [
            {"id": "practice", "course_ids": ["D"], "credits": 3, "deficit": 0},
            {"id": "project", "course_ids": ["C"], "credits": 3, "deficit": 0},
        ])

    def test_without_a_pass_primary_is_protected_and_groups_cannot_both_fill(self):
        data = v1_input(passed=("B", "C", "D"))
        row = review(data)["students"][0]
        # 主修必须保住 B + 6 STEM，因此只有一门可划走；两组无法同时达标。
        self.assertTrue(row["primary"]["eligible"])
        self.assertEqual(row["primary"]["credits"], 6)
        self.assertEqual(len(row["primary"]["course_ids"]), 2)
        secondary = row["secondary"]
        self.assertFalse(secondary["eligible"])
        self.assertEqual(secondary["credits"], 3)
        groups = {g["id"]: g for g in secondary["groups"]}
        # 两种划法的主修/辅修缺口完全相同，按末级字典序：
        # ("practice","C") < ("practice","D")，故 C 划给 practice。
        self.assertEqual(groups["practice"]["course_ids"], ["C"])
        self.assertEqual(groups["practice"]["deficit"], 0)
        self.assertEqual(groups["project"]["course_ids"], [])
        self.assertEqual(groups["project"]["deficit"], 3)
        self.assertEqual(set(row["primary"]["course_ids"]), {"B", "D"})

    def test_input_reordering_does_not_change_report(self):
        first = review(v1_input())
        shuffled = v1_input()
        shuffled["awards"]["groups"].reverse()
        shuffled["attempts"].reverse()
        shuffled["courses"].reverse()
        second = review(shuffled)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_student_ids_reordering_does_not_change_per_student_rows(self):
        data = v1_input(student_ids=("s1", "s2"))
        forward = review(data)
        swapped = copy.deepcopy(data)
        swapped["awards"]["student_ids"] = ["s2", "s1"]
        backward = review(swapped)
        self.assertEqual(
            [r["student_id"] for r in forward["students"]], ["s1", "s2"])
        self.assertEqual(
            [r["student_id"] for r in backward["students"]], ["s1", "s2"])
        self.assertEqual(json.dumps(forward, sort_keys=True),
                         json.dumps(backward, sort_keys=True))


class AssignmentConsistencyTests(unittest.TestCase):
    def test_every_assigned_course_is_traceable_to_evidence_and_disjoint(self):
        data = v1_input()
        row = review(data)["students"][0]
        evidence = by_id(row["evidence_audit"]["selected_passes"])
        assigned = []
        for group in row["secondary"]["groups"]:
            for cid in group["course_ids"]:
                self.assertIn(cid, evidence)  # 报告里每门分配课可在证据行查到
                assigned.append(cid)
        # 整门课不重复出现在两个组，也不留在主修。
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertFalse(set(assigned) & set(row["primary"]["course_ids"]))
        # 主修与辅修课程并集恰为全部已获学分课程。
        self.assertEqual(
            set(assigned) | set(row["primary"]["course_ids"]), set(evidence))
        # 数字不混用：证据总学分 = 主修学分 + 辅修学分。
        self.assertEqual(
            row["evidence_audit"]["credits"],
            row["primary"]["credits"] + row["secondary"]["credits"])

    def test_required_course_with_credit_cannot_leave_primary(self):
        # B 同时被 practice 组列出，但它是主修必修，必须留在主修。
        data = v1_input(groups=[
            {"id": "practice", "course_ids": ["B", "C", "D"],
             "minimum_credits": 3, "source": "any"},
            {"id": "project", "course_ids": ["C"],
             "minimum_credits": 3, "source": "any"},
        ])
        row = review(data)["students"][0]
        self.assertIn("B", row["primary"]["course_ids"])
        for group in row["secondary"]["groups"]:
            self.assertNotIn("B", group["course_ids"])

    def test_course_not_listed_by_a_group_cannot_be_assigned(self):
        data = v1_input(groups=[
            {"id": "solo", "course_ids": ["D"],
             "minimum_credits": 3, "source": "any"}])
        row = review(data)["students"][0]
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], ["D"])
        self.assertEqual(set(row["primary"]["course_ids"]), {"A", "B", "C"})

    def test_one_course_can_never_fill_two_groups(self):
        # 两个组都只列 C；C 只能进其中一个，另一个必须显示缺口。
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["C"], "minimum_credits": 3,
             "source": "any"},
            {"id": "g2", "course_ids": ["C"], "minimum_credits": 3,
             "source": "any"},
        ])
        row = review(data)["students"][0]
        used = [cid for g in row["secondary"]["groups"] for cid in g["course_ids"]]
        self.assertEqual(used, ["C"])
        deficits = {g["id"]: g["deficit"] for g in row["secondary"]["groups"]}
        self.assertEqual(deficits, {"g1": 0, "g2": 3})

    def test_overallocation_still_counts_full_course_credits(self):
        # 组下限 3，但唯一可分配的课为 5 学分：照计全额成本，缺口为 0。
        data = v1_input(
            courses=("A", "B", "C", "D"),
            groups=[{"id": "g1", "course_ids": ["D"],
                     "minimum_credits": 3, "source": "any"}])
        data["courses"][3]["credits"] = 5
        row = review(data)["students"][0]
        group = row["secondary"]["groups"][0]
        self.assertEqual(group["credits"], 5)
        self.assertEqual(group["deficit"], 0)
        self.assertEqual(row["secondary"]["credits"], 5)

    def test_unreachable_group_minimum_is_legal_and_reports_real_gap(self):
        # 下限超过候选课程学分总和仍是合法配置，结果显示真实缺口。
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["D"],
             "minimum_credits": 99, "source": "any"}])
        row = review(data)["students"][0]
        group = row["secondary"]["groups"][0]
        self.assertEqual(group["credits"], 3)
        self.assertEqual(group["deficit"], 96)
        self.assertFalse(row["secondary"]["eligible"])

    def test_large_credit_integers_need_no_expanded_iteration(self):
        # 学分/下限没有小整数上界：用大整数验证算术正确且不按数值展开。
        big = 10 ** 18
        data = v1_input(
            minimum_credits={"STEM": big},
            groups=[{"id": "g1", "course_ids": ["D"],
                     "minimum_credits": big, "source": "any"}])
        for course in data["courses"]:
            course["credits"] = big
        row = review(data)["students"][0]
        self.assertEqual(row["primary"]["credits"], 3 * big)
        self.assertEqual(row["primary"]["credit_deficits"]["STEM"], 0)
        self.assertEqual(row["secondary"]["groups"][0]["credits"], big)
        self.assertEqual(row["secondary"]["groups"][0]["deficit"], big - big)

    def test_zero_credit_category_still_listed_in_primary_breakdown(self):
        data = v1_input()
        data["courses"].append(
            {"id": "ART", "credits": 2, "pass_mark": 60,
             "category": "arts", "prerequisites": []})
        data["program"]["minimum_credits"] = {"STEM": 6, "arts": 2}
        row = review(data)["students"][0]
        self.assertEqual(row["primary"]["credits_by_category"]["arts"], 0)
        self.assertEqual(row["primary"]["credit_deficits"]["arts"], 2)


class SourceRestrictionTests(unittest.TestCase):
    def test_transfer_credit_cannot_fill_exam_only_group(self):
        data = v1_input(groups=[
            {"id": "any-g", "course_ids": ["C", "D"],
             "minimum_credits": 3, "source": "any"},
            {"id": "exam-g", "course_ids": ["C", "D"],
             "minimum_credits": 3, "source": "exam"},
        ])
        # C 的最终证据为转学分，D 为本校考试。
        data["transfer"] = {
            "credit_limit": 3,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2026-01-01"}],
            "options": [
                {"id": "o-c", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["C"], "effective_date": "2026-01-31"}],
        }
        del data["attempts"][2]  # 移除 C 的考试，只保留 A/B/D 考试
        row = review(data)["students"][0]
        sources = {p["course_id"]: p["source"]
                   for p in row["evidence_audit"]["selected_passes"]}
        self.assertEqual(sources["C"], "transfer")
        self.assertEqual(sources["D"], "exam")
        groups = {g["id"]: g for g in row["secondary"]["groups"]}
        # any 组可拿转学分 C；exam 组只能拿考试来源的 D。
        self.assertEqual(groups["any-g"]["course_ids"], ["C"])
        self.assertEqual(groups["exam-g"]["course_ids"], ["D"])

    def test_exam_only_group_stays_empty_when_only_transfer_available(self):
        data = v1_input(passed=("A", "B", "D"), groups=[
            {"id": "exam-g", "course_ids": ["C"],
             "minimum_credits": 3, "source": "exam"}])
        data["transfer"] = {
            "credit_limit": 3,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2026-01-01"}],
            "options": [
                {"id": "o-c", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["C"], "effective_date": "2026-01-31"}],
        }
        row = review(data)["students"][0]
        group = row["secondary"]["groups"][0]
        self.assertEqual(group["course_ids"], [])
        self.assertEqual(group["deficit"], 3)
        # C 已取得学分但只能留在主修。
        self.assertIn("C", row["primary"]["course_ids"])

    def test_early_transfer_unlocked_exam_remains_exam_source(self):
        # 转学分 A 解锁了后来合法的 D 考试；D 的最终来源仍是 exam，
        # 可以填入 exam 限定组。
        data = v1_input(
            courses=("A", "B", "C", "D"),
            passed=("B", "C", "D"),
            groups=[{"id": "exam-g", "course_ids": ["A", "D"],
                     "minimum_credits": 3, "source": "exam"}])
        data["courses"][3]["prerequisites"] = ["A"]  # D 先修 A
        for attempt in data["attempts"]:
            if attempt["course_id"] == "D":
                attempt["date"] = "2026-03-10"
        data["transfer"] = {
            "credit_limit": 3,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2026-01-01"}],
            "options": [
                {"id": "o-a", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["A"], "effective_date": "2026-01-31"}],
        }
        row = review(data)["students"][0]
        sources = {p["course_id"]: p["source"]
                   for p in row["evidence_audit"]["selected_passes"]}
        self.assertEqual(sources["A"], "transfer")
        self.assertEqual(sources["D"], "exam")
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], ["D"])


class V2JointDecisionTests(unittest.TestCase):
    """契约规定的 v2 联动案例：aa/zz 互斥，辅修只接受 D 的考试来源。"""

    def _data(self, zz_date="2026-03-09"):
        data = {
            "as_of": "2026-04-01",
            "courses": [
                stem_course("A"),
                stem_course("B"),
                stem_course("C"),
                stem_course("D", prerequisites=["A"]),
                stem_course("E", prerequisites=["C"]),
            ],
            "students": [{"id": "s1"}],
            "program": {"required_courses": ["B"],
                        "minimum_credits": {"STEM": 6}},
            "attempts": [
                {"id": "b1", "student_id": "s1", "course_id": "B",
                 "date": "2026-03-10", "mark": 80},
                {"id": "d1", "student_id": "s1", "course_id": "D",
                 "date": "2026-03-10", "mark": 80},
                {"id": "e1", "student_id": "s1", "course_id": "E",
                 "date": "2026-03-10", "mark": 80},
            ],
            "transfer": {
                "credit_limit": 3,
                "evidence": [
                    {"id": "ev-aa", "student_id": "s1", "date": "2026-03-01"},
                    {"id": "ev-zz", "student_id": "s1", "date": "2026-03-01"},
                ],
                "options": [
                    {"id": "aa", "student_id": "s1", "evidence_ids": ["ev-aa"],
                     "course_ids": ["C"], "effective_date": "2026-03-09"},
                    {"id": "zz", "student_id": "s1", "evidence_ids": ["ev-zz"],
                     "course_ids": ["A"], "effective_date": zz_date},
                ],
            },
            "awards": {
                "student_ids": ["s1"],
                "groups": [{"id": "minor", "course_ids": ["D"],
                            "minimum_credits": 3, "source": "exam"}],
            },
        }
        return data

    def test_old_audit_command_still_picks_aa(self):
        row = run(self._data())["students"][0]
        self.assertEqual(row["recognition_plan"]["option_ids"], ["aa"])

    def test_joint_review_picks_zz_and_assigns_d_to_minor(self):
        row = review(self._data())["students"][0]
        self.assertEqual(
            row["evidence_audit"]["recognition_plan"]["option_ids"], ["zz"])
        # evidence_audit 是 zz 这套方案自身分配前的原指标，可毕业。
        self.assertTrue(
            row["evidence_audit"]["recognition_plan"]["objective"]
            ["graduation_eligible"])
        sources = {p["course_id"]: p["source"]
                   for p in row["evidence_audit"]["selected_passes"]}
        self.assertEqual(sources["D"], "exam")
        self.assertEqual(row["primary"]["course_ids"], ["A", "B"])
        self.assertTrue(row["primary"]["eligible"])
        self.assertEqual(row["secondary"]["groups"], [
            {"id": "minor", "course_ids": ["D"], "credits": 3, "deficit": 0}])
        self.assertTrue(row["secondary"]["eligible"])

    def test_same_day_zz_cannot_unlock_d_and_minor_cannot_complete(self):
        row = review(self._data(zz_date="2026-03-10"))["students"][0]
        # 联合审核在 D 不可得时回落到旧口径更优的 aa。
        self.assertEqual(
            row["evidence_audit"]["recognition_plan"]["option_ids"], ["aa"])
        audit = {a["attempt_id"]: a["status"]
                 for a in row["evidence_audit"]["attempt_audit"]}
        self.assertEqual(audit["d1"], "blocked_prerequisite")
        self.assertNotIn("D", by_id(row["evidence_audit"]["selected_passes"]))
        self.assertEqual(row["secondary"]["groups"][0]["deficit"], 3)
        self.assertFalse(row["secondary"]["eligible"])
        # 主修仍达标：B、C、E 共 9 学分。
        self.assertTrue(row["primary"]["eligible"])
        self.assertEqual(row["primary"]["course_ids"], ["B", "C", "E"])


class TieBreakTests(unittest.TestCase):
    def test_v1_final_tie_break_is_lexicographic_assignment_list(self):
        # 两个互斥组都只能用 D，且主修状态完全相同；最终按排序后的
        # (组 ID, 课程 ID) 列表取字典序最小，与组在输入中的先后无关。
        data = v1_input(groups=[
            {"id": "g2", "course_ids": ["D"], "minimum_credits": 3,
             "source": "any"},
            {"id": "g1", "course_ids": ["D"], "minimum_credits": 3,
             "source": "any"},
        ])
        row = review(data)["students"][0]
        groups = {g["id"]: g for g in row["secondary"]["groups"]}
        self.assertEqual(groups["g1"]["course_ids"], ["D"])
        self.assertEqual(groups["g2"]["course_ids"], [])
        # 调换输入顺序后结论不变。
        swapped = copy.deepcopy(data)
        swapped["awards"]["groups"].reverse()
        row2 = review(swapped)["students"][0]
        groups2 = {g["id"]: g for g in row2["secondary"]["groups"]}
        self.assertEqual(groups2["g1"]["course_ids"], ["D"])
        self.assertEqual(groups2["g2"]["course_ids"], [])

    def test_v2_pre_audit_tie_break_runs_before_assignment_tail(self):
        # 两个互斥方案带来完全相同的已获课程（只认 X），额度成本相同，
        # 因此按方案 ID 字典序选 aa；分配结果本身两组候选一致。
        data = v1_input(
            passed=("A", "B"),
            groups=[{"id": "g1", "course_ids": ["X"],
                     "minimum_credits": 3, "source": "any"}])
        data["courses"].append(stem_course("X"))
        data["transfer"] = {
            "credit_limit": 6,
            "evidence": [
                {"id": "e1", "student_id": "s1", "date": "2026-01-01"},
                {"id": "e2", "student_id": "s1", "date": "2026-01-01"},
            ],
            "options": [
                {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["X"], "effective_date": "2026-01-31"},
                {"id": "aa", "student_id": "s1", "evidence_ids": ["e2"],
                 "course_ids": ["X"], "effective_date": "2026-01-31"},
            ],
        }
        row = review(data)["students"][0]
        self.assertEqual(
            row["evidence_audit"]["recognition_plan"]["option_ids"], ["aa"])
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], ["X"])

    def test_empty_assignment_wins_when_secondary_cannot_improve(self):
        # 没有任何组能达标时，优先少划学分：全部留在主修，分配为空列表。
        data = v1_input(passed=("A", "B"), groups=[
            {"id": "g1", "course_ids": ["A", "B"],
             "minimum_credits": 99, "source": "any"}])
        row = review(data)["students"][0]
        self.assertEqual(row["secondary"]["credits"], 0)
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], [])
        self.assertEqual(row["primary"]["course_ids"], ["A", "B"])


class EdgeCaseTests(unittest.TestCase):
    def test_empty_student_list_emits_empty_array(self):
        data = v1_input(student_ids=())
        data["awards"]["student_ids"] = []
        report = review(data)
        self.assertEqual(report, {"schema_version": 1,
                                  "as_of": "2026-04-01", "students": []})

    def test_unlisted_student_still_validated_but_not_reported(self):
        data = v1_input(student_ids=("s1", "s2"))
        data["awards"]["student_ids"] = ["s1"]
        # s2 有一条结构非法的考试仍会让整份输入失败。
        data["attempts"].append(
            {"id": "bad", "student_id": "s2", "course_id": "C",
             "date": "not-a-date", "mark": 80})
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            review(data)
        good = v1_input(student_ids=("s1", "s2"))
        good["awards"]["student_ids"] = ["s2"]
        report = review(good)
        self.assertEqual([r["student_id"] for r in report["students"]], ["s2"])

    def test_no_assignable_courses_leaves_all_in_primary(self):
        # 组引用一门无人取得学分的课。
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["ZZZ"],
             "minimum_credits": 3, "source": "any"}])
        data["courses"].append(stem_course("ZZZ"))
        row = review(data)["students"][0]
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], [])
        self.assertEqual(row["secondary"]["groups"][0]["deficit"], 3)
        self.assertEqual(
            row["primary"]["course_ids"], ["A", "B", "C", "D"])

    def test_partial_secondary_reports_per_group_gaps(self):
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["D"], "minimum_credits": 6,
             "source": "any"},
            {"id": "g2", "course_ids": ["C"], "minimum_credits": 3,
             "source": "any"},
        ])
        row = review(data)["students"][0]
        groups = {g["id"]: g for g in row["secondary"]["groups"]}
        self.assertEqual(groups["g1"]["deficit"], 3)
        self.assertEqual(groups["g2"]["deficit"], 0)
        self.assertFalse(row["secondary"]["eligible"])

    def test_joint_eligibility_requires_both_sides(self):
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["D"], "minimum_credits": 3,
             "source": "any"}])
        row = review(data)["students"][0]
        self.assertTrue(row["primary"]["eligible"])
        self.assertTrue(row["secondary"]["eligible"])

    def test_future_transfer_options_are_validated_but_never_used(self):
        data = v1_input(groups=[
            {"id": "g1", "course_ids": ["C"], "minimum_credits": 3,
             "source": "any"}])
        data["transfer"] = {
            "credit_limit": 3,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2030-01-01"}],
            "options": [
                {"id": "future", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["C"], "effective_date": "2030-02-01"}],
        }
        row = review(data)["students"][0]
        self.assertEqual(
            row["evidence_audit"]["recognition_plan"]["option_ids"], [])
        # C 的考试证据仍在，可正常分配。
        self.assertEqual(row["secondary"]["groups"][0]["course_ids"], ["C"])


class AwardsValidationTests(unittest.TestCase):
    def assert_rejected(self, mutate, fragment):
        data = v1_input()
        mutate(data)
        with self.assertRaisesRegex(ValueError, fragment):
            review(data)

    def test_missing_awards_node_rejected(self):
        data = v1_input()
        del data["awards"]
        with self.assertRaisesRegex(ValueError, "awards"):
            review(data)

    def test_missing_fields_rejected(self):
        def drop_ids(d):
            del d["awards"]["student_ids"]
        def drop_groups(d):
            del d["awards"]["groups"]
        self.assert_rejected(drop_ids, "student_ids")
        self.assert_rejected(drop_groups, "groups")

    def test_student_id_rules(self):
        def too_many(d):
            d["students"] = [{"id": f"s{i}"} for i in range(13)]
            d["awards"]["student_ids"] = [f"s{i}" for i in range(13)]
            d["attempts"] = []
        self.assert_rejected(too_many, "12 students")
        self.assert_rejected(
            lambda d: d["awards"]["student_ids"].append("s1"), "unique")
        self.assert_rejected(
            lambda d: d["awards"]["student_ids"].append("ghost"), "unknown")
        self.assert_rejected(
            lambda d: d["awards"]["student_ids"].append(""), "nonempty")
        self.assert_rejected(
            lambda d: d["awards"].__setitem__("student_ids", "s1"), "array")

    def test_group_count_and_shape_rules(self):
        self.assert_rejected(lambda d: d["awards"].__setitem__("groups", []),
                             "1..4")
        def five_groups(d):
            d["awards"]["groups"] = [
                {"id": f"g{i}", "course_ids": ["D"],
                 "minimum_credits": 3, "source": "any"} for i in range(5)]
        self.assert_rejected(five_groups, "1..4")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__("source", "nope"),
            "any or exam")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__(
                "minimum_credits", True), "integer")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__(
                "minimum_credits", 0), "integer")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__(
                "course_ids", []), "nonempty")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__(
                "course_ids", ["C", "C"]), "unique")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__(
                "course_ids", ["NOPE"]), "unknown")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][1].__setitem__("id", "practice"),
            "duplicate")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].__setitem__("extra", 1),
            "exactly")
        self.assert_rejected(
            lambda d: d["awards"]["groups"][0].pop("source"), "exactly")

    def test_course_membership_and_distinct_course_limits(self):
        def eleven_distinct(d):
            d["courses"] = [stem_course(chr(ord("A") + i)) for i in range(11)]
            d["awards"]["groups"] = [
                {"id": "g1",
                 "course_ids": [c["id"] for c in d["courses"]],
                 "minimum_credits": 3, "source": "any"}]
        self.assert_rejected(eleven_distinct, "10 distinct")

        def three_memberships(d):
            d["awards"]["groups"] = [
                {"id": f"g{i}", "course_ids": ["D"],
                 "minimum_credits": 3, "source": "any"} for i in range(3)]
        self.assert_rejected(three_memberships, "at most 2")

    def test_effective_option_limit_only_for_listed_students(self):
        def seven_active(d):
            d["transfer"] = {
                "credit_limit": 100,
                "evidence": [{"id": "e1", "student_id": "s1",
                              "date": "2026-01-01"}],
                "options": [
                    {"id": f"o{i}", "student_id": "s1",
                     "evidence_ids": ["e1"], "course_ids": ["A"],
                     "effective_date": "2026-01-31"} for i in range(7)],
            }
        self.assert_rejected(seven_active, "6")

    def test_seven_future_options_remain_under_original_rules(self):
        # 未生效方案不受 6 个的小批次边界限制，仍按原每生 12 个的规则校验。
        data = v1_input()
        data["transfer"] = {
            "credit_limit": 100,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2030-01-01"}],
            "options": [
                {"id": f"o{i}", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["A"], "effective_date": "2030-02-01"}
                for i in range(7)],
        }
        report = review(data)  # 不抛异常
        self.assertEqual(
            report["students"][0]["evidence_audit"]["recognition_plan"]
            ["option_ids"], [])

    def test_option_limit_ignores_unlisted_students(self):
        data = v1_input(student_ids=("s1", "s2"))
        data["awards"]["student_ids"] = ["s1"]
        data["transfer"] = {
            "credit_limit": 100,
            "evidence": [{"id": "e2", "student_id": "s2",
                          "date": "2026-01-01"}],
            "options": [
                {"id": f"o{i}", "student_id": "s2", "evidence_ids": ["e2"],
                 "course_ids": ["A"], "effective_date": "2026-01-31"}
                for i in range(7)],
        }
        review(data)  # s2 不在名单内，不受 6 个边界约束


class OriginalCommandCompatibilityTests(unittest.TestCase):
    def test_awards_node_does_not_change_credit_audit(self):
        data = v1_input()
        plain = {k: v for k, v in data.items() if k != "awards"}
        self.assertEqual(
            json.dumps(run(plain), sort_keys=True),
            json.dumps(run(data), sort_keys=True))

    def test_v2_audit_unchanged_with_awards_node(self):
        data = V2JointDecisionTests()._data()
        plain = {k: v for k, v in data.items() if k != "awards"}
        self.assertEqual(
            json.dumps(run(plain), sort_keys=True),
            json.dumps(run(data), sort_keys=True))

    def test_evidence_audit_uses_original_student_row_structure(self):
        row = review(v1_input())["students"][0]
        self.assertEqual(
            set(row["evidence_audit"]),
            {"student_id", "selected_passes", "attempt_audit",
             "ignored_future_attempts", "credits", "credits_by_category",
             "credited_weighted_mark", "missing_required", "credit_deficits",
             "graduation_eligible", "eligible_uncredited_courses"})
        data = V2JointDecisionTests()._data()
        v2row = review(data)["students"][0]
        self.assertIn("recognition_plan", v2row["evidence_audit"])
        for pass_row in v2row["evidence_audit"]["selected_passes"]:
            self.assertIn("source", pass_row)


def invoke(source, output):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run(
        [sys.executable, "-m", "registrar.awards_cli",
         "--source", str(source), "--output", str(output)],
        cwd=REPO, env=env, capture_output=True, text=True)


class AwardsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_console_script_is_declared(self):
        pyproject = (REPO / "pyproject.toml").read_text()
        self.assertIn('credit-awards = "registrar.awards_cli:main"', pyproject)

    def test_report_written_atomically(self):
        source = self.dir / "in.json"
        output = self.dir / "nested" / "awards.json"
        source.write_text(json.dumps(v1_input()))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("'joint_eligible': 1", proc.stdout)

    def test_empty_students_via_cli(self):
        data = v1_input()
        data["awards"]["student_ids"] = []
        source = self.dir / "empty.json"
        output = self.dir / "out.json"
        source.write_text(json.dumps(data))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(output.read_text())["students"], [])

    def test_invalid_input_exits_2_and_creates_nothing(self):
        bad = v1_input()
        bad["awards"]["groups"][0]["minimum_credits"] = True
        source = self.dir / "bad.json"
        output = self.dir / "out.json"
        source.write_text(json.dumps(bad))
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("minimum_credits", proc.stderr)
        self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        source = self.dir / "in.json"
        output = self.dir / "out.json"
        source.write_text(json.dumps(v1_input()))
        output.write_text("KEEP ME")
        proc = invoke(source, output)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(output.read_text(), "KEEP ME")


if __name__ == "__main__":
    unittest.main()
