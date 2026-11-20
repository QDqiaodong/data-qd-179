import copy
import json
import unittest
from registrar.service import run
from tests.test_transfer_validation import base_input, transfer_node, with_transfer


def spec_example(**overrides):
    """The worked example from the v2 contract (A/B root STEM 3, C STEM 4
    prereq A, D arts 2; options aa->B and zz->A from one evidence item)."""
    data = base_input()
    data["program"]["required_courses"] = []
    node = transfer_node()
    node["credit_limit"] = 3
    node.update(overrides)
    data["transfer"] = node
    return data


def by_id(rows):
    return {row["course_id"]: row for row in rows}


class SpecExampleTests(unittest.TestCase):
    def test_holistic_plan_picks_zz_because_it_unblocks_c(self):
        result = run(spec_example())
        s1 = result["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["zz"])
        self.assertEqual(s1["credits"], 9)
        self.assertTrue(s1["graduation_eligible"])
        self.assertEqual(s1["credited_weighted_mark"], "230/3")
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["A"], {
            "course_id": "A", "attempt_id": None, "mark": None,
            "credits": 3, "source": "transfer", "option_id": "zz"})
        self.assertEqual(passes["C"]["source"], "exam")
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 3)
        self.assertEqual(s1["recognition_plan"]["objective"], {
            "graduation_eligible": True, "required_covered": 0,
            "capped_category_credits": 9, "credits": 9})
        # Summary reflects the chosen plan's full re-audit.
        self.assertEqual(result["summary"]["credited_units"], 9)
        self.assertEqual(result["summary"]["blocked_attempts"], 0)

    def test_same_day_effective_date_does_not_unlock_exam(self):
        data = spec_example()
        data["transfer"]["options"][1]["effective_date"] = "2026-03-01"
        s1 = run(data)["students"][0]
        audit = {a["attempt_id"]: a for a in s1["attempt_audit"]}
        self.assertEqual(audit["c1"]["status"], "blocked_prerequisite")
        self.assertEqual(audit["c1"]["missing_prerequisites"], ["A"])
        # zz no longer beats aa: both yield 5 credits, tie-break is ID order.
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["aa"])
        self.assertEqual(s1["recognition_plan"]["objective"]["credits"], 5)
        self.assertEqual(s1["credits"], 5)
        self.assertEqual(s1["credited_weighted_mark"], "70")

    def test_later_exam_becomes_source_without_changing_plan_or_cost(self):
        data = spec_example()
        data["attempts"].append(
            {"id": "a9", "student_id": "s1", "course_id": "A",
             "date": "2026-03-15", "mark": 95})
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["zz"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 3)
        self.assertEqual(s1["credits"], 9)
        self.assertEqual(s1["credited_weighted_mark"], "745/9")
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["A"]["source"], "exam")
        self.assertEqual(passes["A"]["attempt_id"], "a9")
        self.assertIsNone(passes["A"]["option_id"])

    def test_weighted_mark_null_when_only_transfer_credits(self):
        data = spec_example()
        data["attempts"] = []
        node = data["transfer"]
        node["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertIsNone(s1["credited_weighted_mark"])
        self.assertEqual(s1["credits"], 3)


class PlanFeasibilityTests(unittest.TestCase):
    def test_option_is_atomic_all_or_nothing(self):
        data = spec_example()
        # One option covers A and D together; selecting it must bring both.
        data["transfer"]["options"] = [
            {"id": "combo", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "D"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 5
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["combo"])
        courses = {p["course_id"] for p in s1["selected_passes"]}
        self.assertEqual(courses, {"A", "C", "D"})

    def test_atomic_option_rejected_when_budget_covers_only_part(self):
        data = spec_example()
        data["transfer"]["options"] = [
            {"id": "combo", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "D"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 4  # needs 5; no partial acceptance
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], [])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 0)
        self.assertEqual(s1["credits"], 2)  # only the D exam

    def test_evidence_conflict_makes_options_mutually_exclusive(self):
        data = spec_example()  # aa and zz share e1
        s1 = run(data)["students"][0]
        self.assertEqual(len(s1["recognition_plan"]["option_ids"]), 1)

    def test_target_course_conflict_excludes_combination(self):
        data = spec_example()
        data["transfer"]["evidence"].append(
            {"id": "e3", "student_id": "s1", "date": "2026-01-01"})
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["A", "B"], "effective_date": "2026-02-01"}]
        data["transfer"]["credit_limit"] = 10
        s1 = run(data)["students"][0]
        # Both target A, so they cannot combine; o2 alone covers more.
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o2"])

    def test_independent_options_combine_within_budget(self):
        data = spec_example()
        data["transfer"]["evidence"].append(
            {"id": "e3", "student_id": "s1", "date": "2026-01-01"})
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 6
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o1", "o2"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 6)

    def test_budget_blocks_second_option_even_when_courses_disjoint(self):
        data = spec_example()
        data["transfer"]["evidence"].append(
            {"id": "e3", "student_id": "s1", "date": "2026-01-01"})
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 3
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o1"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 3)

    def test_full_quota_charged_even_with_existing_exam_pass(self):
        # The only graduating plan transfers A and D, but D already has a
        # legal local exam pass. The option must still charge its full cost
        # (3 + 2 = 5), and D's final source stays the exam with its mark.
        data = spec_example()
        data["program"]["minimum_credits"] = {"STEM": 3, "arts": 2}
        data["transfer"]["credit_limit"] = 5
        data["transfer"]["options"] = [
            {"id": "ad", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "D"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["ad"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 5)
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["D"]["source"], "exam")
        self.assertEqual(passes["D"]["mark"], 70)
        self.assertEqual(passes["A"]["source"], "transfer")
        self.assertTrue(s1["graduation_eligible"])

    def test_budget_rejects_plan_when_existing_pass_does_not_refund_quota(self):
        # Same content, but a limit of 4 cannot cover the full 5-credit charge
        # even though D would come from the exam anyway.
        data = spec_example()
        data["program"]["minimum_credits"] = {"STEM": 3, "arts": 2}
        data["transfer"]["credit_limit"] = 4
        data["transfer"]["options"] = [
            {"id": "ad", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "D"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], [])
        self.assertEqual(s1["credits"], 2)

    def test_empty_plan_is_always_allowed_and_default(self):
        data = spec_example()
        data["transfer"]["options"] = []
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"], {
            "option_ids": [], "charged_credits": 0,
            "objective": {"graduation_eligible": False, "required_covered": 0,
                          "capped_category_credits": 2, "credits": 2}})
        self.assertTrue(all(p["source"] == "exam" for p in s1["selected_passes"]))

    def test_no_available_options_when_all_future(self):
        data = spec_example()
        for option in data["transfer"]["options"]:
            option["effective_date"] = "2030-01-01"
        result = run(data)
        s1 = result["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], [])
        self.assertEqual(result["summary"]["blocked_attempts"], 1)

    def test_students_are_optimized_independently(self):
        data = spec_example()
        # s2 gets its own disjoint evidence and option for D (arts 2).
        data["transfer"]["options"].append(
            {"id": "s2d", "student_id": "s2", "evidence_ids": ["e2"],
             "course_ids": ["D"], "effective_date": "2026-01-31"})
        result = run(data)
        plans = {r["student_id"]: r["recognition_plan"]["option_ids"]
                 for r in result["students"]}
        self.assertEqual(plans["s1"], ["zz"])
        self.assertEqual(plans["s2"], ["s2d"])

    def test_credit_limit_is_per_student(self):
        data = spec_example()
        data["attempts"] = []
        data["courses"].append(
            {"id": "E", "credits": 5, "pass_mark": 60, "category": "STEM",
             "prerequisites": []})
        data["transfer"]["credit_limit"] = 3
        data["transfer"]["options"] = [
            {"id": "p1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-02-01"},
            {"id": "p2", "student_id": "s2", "evidence_ids": ["e2"],
             "course_ids": ["A"], "effective_date": "2026-02-01"},
            {"id": "p3", "student_id": "s2", "evidence_ids": ["e2"],
             "course_ids": ["E"], "effective_date": "2026-02-01"}]
        result = run(data)
        plans = {r["student_id"]: r["recognition_plan"]["option_ids"]
                 for r in result["students"]}
        # p3 costs 5 > 3 for s2; s1's plan is unaffected by s2's rejection.
        self.assertEqual(plans["s1"], ["p1"])
        self.assertEqual(plans["s2"], ["p2"])


class ChronologyTests(unittest.TestCase):
    def test_blocked_high_mark_never_unlocks_downstream_or_repairs(self):
        data = spec_example()
        # Remove options that could rescue A; C stays blocked despite mark 80.
        data["transfer"]["options"] = []
        s1 = run(data)["students"][0]
        self.assertNotIn("C", by_id(s1["selected_passes"]))
        audit = {a["attempt_id"]: a for a in s1["attempt_audit"]}
        self.assertEqual(audit["c1"]["status"], "blocked_prerequisite")

    def test_later_transfer_does_not_repair_earlier_blocked_exam(self):
        data = spec_example()
        data["transfer"]["options"][1]["effective_date"] = "2026-03-15"
        s1 = run(data)["students"][0]
        audit = {a["attempt_id"]: a for a in s1["attempt_audit"]}
        self.assertEqual(audit["c1"]["status"], "blocked_prerequisite")
        self.assertNotIn("C", by_id(s1["selected_passes"]))

    def test_transfer_skips_target_prerequisite_check(self):
        # C normally requires A; recognizing C directly needs no A pass.
        data = spec_example()
        data["transfer"]["options"] = [
            {"id": "direct-c", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["C"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 4
        s1 = run(data)["students"][0]
        self.assertIn("C", by_id(s1["selected_passes"]))
        self.assertEqual(s1["credits"], 6)

    def test_multi_course_option_takes_effect_simultaneously(self):
        # A and C in one option: C must not demand an earlier A exam.
        data = spec_example()
        data["attempts"] = [a for a in data["attempts"] if a["course_id"] != "C"]
        data["transfer"]["options"] = [
            {"id": "ac", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "C"], "effective_date": "2026-01-31"}]
        data["transfer"]["credit_limit"] = 7
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["ac"])
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["A"]["option_id"], "ac")
        self.assertEqual(passes["C"]["option_id"], "ac")

    def test_early_transfer_kept_as_prereq_fact_even_when_exam_wins_course(self):
        data = spec_example()
        # A later exam becomes the source of A, but zz still unlocked C.
        data["attempts"].append(
            {"id": "a9", "student_id": "s1", "course_id": "A",
             "date": "2026-03-15", "mark": 95})
        s1 = run(data)["students"][0]
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["A"]["source"], "exam")
        self.assertEqual(passes["C"]["source"], "exam")
        self.assertEqual(s1["credits"], 9)

    def test_highest_exam_mark_replaces_transfer_as_course_source(self):
        data = spec_example()
        data["attempts"] += [
            {"id": "a-low", "student_id": "s1", "course_id": "A",
             "date": "2026-02-10", "mark": 61},
            {"id": "a-high", "student_id": "s1", "course_id": "A",
             "date": "2026-02-20", "mark": 99}]
        s1 = run(data)["students"][0]
        row = by_id(s1["selected_passes"])["A"]
        self.assertEqual(row["source"], "exam")
        self.assertEqual(row["attempt_id"], "a-high")
        self.assertEqual(row["mark"], 99)
        # 99*3 + 80*4 + 70*2 = 757 over 9 credits.
        self.assertEqual(s1["credited_weighted_mark"], "757/9")

    def test_failed_and_future_exams_do_not_become_credits(self):
        data = spec_example()
        data["attempts"].append(
            {"id": "f1", "student_id": "s1", "course_id": "A",
             "date": "2026-05-01", "mark": 30})
        data["attempts"].append(
            {"id": "fut", "student_id": "s1", "course_id": "A",
             "date": "2027-01-01", "mark": 90})
        s1 = run(data)["students"][0]
        statuses = {a["attempt_id"]: a["status"] for a in s1["attempt_audit"]}
        self.assertEqual(statuses["f1"], "failed")
        self.assertNotIn("fut", statuses)
        self.assertEqual(s1["ignored_future_attempts"], 1)
        row = by_id(s1["selected_passes"])["A"]
        self.assertEqual(row["source"], "transfer")


class ObjectiveTieBreakTests(unittest.TestCase):
    def _program_input(self, minimums, required):
        data = spec_example()
        data["program"]["minimum_credits"] = minimums
        data["program"]["required_courses"] = required
        data["attempts"] = []
        data["transfer"]["evidence"].append(
            {"id": "e3", "student_id": "s1", "date": "2026-01-01"})
        return data

    def test_graduation_dominates_credit_count(self):
        # Mutually exclusive plans (one shared evidence item). o1 gives more
        # raw credits but never satisfies arts; o2 meets every minimum and
        # graduates with fewer total credits.
        data = self._program_input({"STEM": 3, "arts": 2}, [])
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B", "C"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "D"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o2"])
        objective = s1["recognition_plan"]["objective"]
        self.assertTrue(objective["graduation_eligible"])
        self.assertEqual(objective["credits"], 5)

    def test_required_covered_beats_capped_categories(self):
        # Neither plan can graduate (arts unreachable); both cap STEM at 7,
        # but o2 covers the required course A while o1 covers neither.
        data = self._program_input({"STEM": 7, "arts": 2}, ["A"])
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B", "C"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["A", "C"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o2"])
        self.assertEqual(
            s1["recognition_plan"]["objective"]["required_covered"], 1)
        self.assertEqual(
            s1["recognition_plan"]["objective"]["capped_category_credits"], 7)

    def test_capped_category_credits_ignores_excess_above_minimum(self):
        # Same required coverage; o2 has more raw STEM credits but the capped
        # values are identical, so later tie-breakers (not raw credits beyond
        # the cap) decide. Both plans stay at 3 capped STEM credits.
        data = self._program_input({"STEM": 3}, [])
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["A", "B"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        # Both graduate and cap at 3; o1 has fewer total credits, but o2 wins
        # on total credits (3 vs 6) since caps tie.
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o2"])
        self.assertEqual(
            s1["recognition_plan"]["objective"]["capped_category_credits"], 3)
        self.assertEqual(s1["recognition_plan"]["objective"]["credits"], 6)

    def test_total_credits_breaks_cap_tie(self):
        # Shared evidence makes the plans exclusive. Both cap STEM at 3, but
        # o2 also brings an extra 4-credit course, so o2 wins on total credits.
        data = self._program_input({"STEM": 3}, [])
        data["courses"].append(
            {"id": "E", "credits": 4, "pass_mark": 60, "category": "STEM",
             "prerequisites": []})
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "o1", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "o2", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A", "E"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["o2"])
        self.assertEqual(
            s1["recognition_plan"]["objective"]["capped_category_credits"], 3)
        self.assertEqual(s1["recognition_plan"]["objective"]["credits"], 7)

    def test_lower_cost_wins_when_outcomes_tie(self):
        # Two ways to get exactly the same audited outcome (A recognized),
        # but one option costs extra quota by bundling an already-exam-passed
        # course whose final source stays the exam.
        data = self._program_input({"STEM": 7}, [])
        data["attempts"] = [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-01-15", "mark": 90}]
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "cheap", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "pricey", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["A", "B"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["cheap"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 3)
        # B still comes from the exam and keeps its mark.
        passes = by_id(s1["selected_passes"])
        self.assertEqual(passes["B"]["source"], "exam")
        self.assertEqual(passes["B"]["mark"], 90)

    def test_fewer_options_win_on_count_tie(self):
        # A single bundled option and two separate options yield identical
        # audited courses; equal cost, so the smaller option set wins.
        data = self._program_input({"STEM": 7}, [])
        data["transfer"]["evidence"].append(
            {"id": "e4", "student_id": "s1", "date": "2026-01-01"})
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "bundle", "student_id": "s1",
             "evidence_ids": ["e1", "e3", "e4"],
             "course_ids": ["A", "B"], "effective_date": "2026-01-31"},
            {"id": "p1", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "p2", "student_id": "s1", "evidence_ids": ["e4"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["bundle"])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 6)

    def test_final_lexicographic_tie_break(self):
        # Shared evidence => exclusive; identical outcomes decide by ID order.
        data = self._program_input({"STEM": 3}, [])
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "aa", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["aa"])
        # Input order is irrelevant: reorder and expect the same plan.
        data["transfer"]["options"].reverse()
        s1b = run(data)["students"][0]
        self.assertEqual(s1b["recognition_plan"]["option_ids"], ["aa"])

    def test_multi_option_set_sorted_before_lexicographic_compare(self):
        data = self._program_input({"STEM": 7}, [])
        data["transfer"]["credit_limit"] = 20
        data["transfer"]["options"] = [
            {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
            {"id": "aa", "student_id": "s1", "evidence_ids": ["e3"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}]
        s1 = run(data)["students"][0]
        # Both selected together; reported IDs ascending regardless of order.
        self.assertEqual(s1["recognition_plan"]["option_ids"], ["aa", "zz"])


class StabilityAndReportTests(unittest.TestCase):
    def test_input_reordering_does_not_change_plan_or_report(self):
        first = run(spec_example())
        shuffled = spec_example()
        shuffled["transfer"]["options"].reverse()
        shuffled["transfer"]["evidence"].reverse()
        shuffled["attempts"].reverse()
        second = run(shuffled)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True))

    def test_selected_passes_sorted_by_course_id(self):
        s1 = run(spec_example())["students"][0]
        ids = [p["course_id"] for p in s1["selected_passes"]]
        self.assertEqual(ids, sorted(ids))

    def test_recognition_plan_fields_for_empty_plan(self):
        data = spec_example()
        data["transfer"]["options"] = []
        s1 = run(data)["students"][0]
        self.assertEqual(s1["recognition_plan"]["option_ids"], [])
        self.assertEqual(s1["recognition_plan"]["charged_credits"], 0)
        for pass_row in s1["selected_passes"]:
            self.assertEqual(pass_row["source"], "exam")
            self.assertIsNone(pass_row["option_id"])

    def test_no_phantom_attempts_in_audit(self):
        data = spec_example()
        s1 = run(data)["students"][0]
        audited = {a["attempt_id"] for a in s1["attempt_audit"]}
        self.assertEqual(audited, {"d1", "c1"})

    def test_attempt_audit_identical_for_dominated_plans_inputs(self):
        # The selected plan's audit is the re-audited one, not a v1 leftover.
        data = spec_example()
        s1 = run(data)["students"][0]
        statuses = [(a["attempt_id"], a["status"]) for a in s1["attempt_audit"]]
        self.assertIn(("c1", "passed"), statuses)


if __name__ == "__main__":
    unittest.main()
