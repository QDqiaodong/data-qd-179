import copy
import json
import unittest
from registrar.service import run
from registrar.planning import plan


COURSES = [
    {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
     "prerequisites": []},
    {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
     "prerequisites": []},
    {"id": "C", "credits": 4, "pass_mark": 60, "category": "STEM",
     "prerequisites": ["A", "B"]},
    {"id": "D", "credits": 2, "pass_mark": 60, "category": "arts",
     "prerequisites": []},
]


def base(courses=None, program=None, students=None, attempts=None,
         as_of="2026-06-01"):
    return {
        "as_of": as_of,
        "courses": copy.deepcopy(COURSES if courses is None else courses),
        "students": copy.deepcopy(
            [{"id": "s1"}, {"id": "s2"}] if students is None else students),
        "program": ({"required_courses": ["A", "B"],
                     "minimum_credits": {"STEM": 6}}
                    if program is None else copy.deepcopy(program)),
        "attempts": copy.deepcopy([] if attempts is None else attempts),
    }


def term(tid, start, end, limit, offerings):
    return {"id": tid, "start": start, "end": end,
            "credit_limit": limit,
            "offerings": [{"course_id": cid, "capacity": cap}
                          for cid, cap in offerings]}


def with_planning(data, student_ids, terms):
    data = copy.deepcopy(data)
    data["planning"] = {"student_ids": student_ids, "terms": terms}
    return data


def students_by_id(report):
    return {row["student_id"]: row for row in report["students"]}


def objective(report):
    return report["objective"]


class DistinguishingExampleTests(unittest.TestCase):
    def data(self):
        data = base(program={"required_courses": ["A", "B"],
                             "minimum_credits": {"STEM": 6}})
        data["attempts"] = [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}]
        return with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 3, [("A", 1), ("B", 1)]),
        ])

    def test_group_outcome_beats_early_graduation_of_priority_student(self):
        report = plan(self.data())
        rows = students_by_id(report)
        # s2 takes the only t1 A seat; both graduate at t2 (9 credits, 3 seats).
        self.assertEqual(objective(report)["graduates"], 2)
        self.assertEqual(objective(report)["graduation_flags"], [1, 1])
        self.assertEqual(objective(report)["completion_indices"], [2, 2])
        self.assertEqual(objective(report)["planned_credits"], 9)
        self.assertEqual(objective(report)["enrollments"], 3)
        self.assertEqual(rows["s1"]["status"], "scheduled")
        self.assertEqual(rows["s2"]["status"], "scheduled")
        self.assertEqual(rows["s1"]["planned_courses"],
                         [{"term_id": "t2", "course_id": "A"}])
        self.assertEqual(rows["s2"]["planned_courses"],
                         [{"term_id": "t1", "course_id": "A"},
                          {"term_id": "t2", "course_id": "B"}])

    def test_enrollments_and_seat_usage_shapes(self):
        report = plan(self.data())
        t1, t2 = report["terms"]
        self.assertEqual(t1["term_id"], "t1")
        self.assertEqual(t1["enrollments"],
                         [{"student_id": "s2", "course_id": "A"}])
        self.assertEqual(t1["seat_usage"],
                         [{"course_id": "A", "used": 1, "capacity": 1}])
        self.assertEqual(t2["enrollments"],
                         [{"student_id": "s1", "course_id": "A"},
                          {"student_id": "s2", "course_id": "B"}])
        self.assertEqual(t2["seat_usage"],
                         [{"course_id": "A", "used": 1, "capacity": 1},
                          {"course_id": "B", "used": 1, "capacity": 1}])

    def test_terms_sort_by_start_even_when_input_shuffled(self):
        data = self.data()
        data["planning"]["terms"].reverse()
        report = plan(data)
        self.assertEqual([t["term_id"] for t in report["terms"]], ["t1", "t2"])

    def test_fixture_file_matches_contract_example(self):
        from pathlib import Path
        from registrar.io import load
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "input_plan.json"
        report = plan(load(fixture))
        self.assertEqual(report["objective"]["graduates"], 2)
        # s3 is audited in the baseline but takes no future seats.
        baseline_ids = {row["student_id"] for row in report["baseline_audit"]["students"]}
        self.assertEqual(baseline_ids, {"s1", "s2", "s3"})
        self.assertEqual({row["student_id"] for row in report["students"]}, {"s1", "s2"})


class PrerequisiteChainTests(unittest.TestCase):
    def data(self, **overrides):
        program = {"required_courses": ["C"],
                   "minimum_credits": {"STEM": 10}}
        terms = [
            term("t1", "2026-09-01", "2026-12-31", 10, [("A", 2), ("B", 2)]),
            term("t2", "2027-02-01", "2027-05-31", 10, [("A", 2), ("B", 2), ("C", 2)]),
            term("t3", "2027-09-01", "2027-12-31", 10, [("C", 2)]),
        ]
        terms[2]["offerings"] = [{"course_id": "C", "capacity": 2}]
        data = base(program=program, **overrides)
        return with_planning(data, ["s1"], terms)

    def test_same_term_courses_never_unlock_each_other(self):
        report = plan(self.data())
        row = students_by_id(report)["s1"]
        # C needs both A and B from strictly earlier terms; earliest is t2.
        self.assertEqual(row["graduation_term_index"], 2)
        self.assertEqual(row["graduation_term_id"], "t2")
        planned = [(item["term_id"], item["course_id"]) for item in row["planned_courses"]]
        self.assertEqual(planned,
                         [("t1", "A"), ("t1", "B"), ("t2", "C")])

    def test_chain_spanning_three_terms_respects_strict_ordering(self):
        # A only in t1, B only in t2, C only in t3 -> graduation at t3.
        data = self.data()
        data["planning"]["terms"] = [
            term("t1", "2026-09-01", "2026-12-31", 10, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 10, [("B", 1)]),
            term("t3", "2027-09-01", "2027-12-31", 10, [("C", 1)]),
        ]
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["graduation_term_index"], 3)
        self.assertEqual(row["graduation_term_id"], "t3")

    def test_chain_impossible_when_head_course_never_offered(self):
        data = self.data()
        data["planning"]["terms"][0]["offerings"] = [
            {"course_id": "B", "capacity": 2}]
        data["planning"]["terms"][1]["offerings"] = [
            {"course_id": "B", "capacity": 2},
            {"course_id": "C", "capacity": 2}]
        row = students_by_id(plan(data))["s1"]
        self.assertFalse(row["individual_graduation_possible"])
        self.assertEqual(row["status"], "unreachable")
        self.assertIsNone(row["graduation_term_index"])
        self.assertIsNone(row["graduation_term_id"])

    def test_chain_cannot_unlock_within_one_term_even_when_seats_exist(self):
        # A, B, C all offered only in t1: C's prerequisites A and B are not
        # completed at any strictly earlier term, so C stays out of reach.
        data = base(program={"required_courses": ["C"],
                             "minimum_credits": {"STEM": 10}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 10,
             [("A", 2), ("B", 2), ("C", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["status"], "unreachable")
        self.assertFalse(row["individual_graduation_possible"])
        self.assertEqual(
            [item["course_id"] for item in row["planned_courses"]], [])

    def test_credit_limit_blocks_both_prerequisites_in_one_term(self):
        data = self.data()
        data["planning"]["terms"][0]["credit_limit"] = 3  # only one of A/B
        row = students_by_id(plan(data))["s1"]
        # t1: one prereq, t2: the other prereq, C can only be taken in t3.
        self.assertEqual(row["graduation_term_index"], 3)
        self.assertTrue(row["individual_graduation_possible"])


class CapacityDeferralTests(unittest.TestCase):
    def test_both_individually_possible_but_only_one_seat(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)])])
        report = plan(data)
        rows = students_by_id(report)
        self.assertEqual(objective(report)["graduates"], 1)
        self.assertEqual(objective(report)["graduation_flags"], [1, 0])
        self.assertEqual(rows["s1"]["status"], "scheduled")
        self.assertEqual(rows["s2"]["status"], "capacity_deferred")
        self.assertTrue(rows["s2"]["individual_graduation_possible"])
        self.assertEqual(objective(report)["completion_indices"], [1, 2])

    def test_zero_capacity_is_unusable_not_an_open_seat(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 0)])])
        report = plan(data)
        row = students_by_id(report)["s1"]
        self.assertFalse(row["individual_graduation_possible"])
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(report["terms"][0]["seat_usage"],
                         [{"course_id": "A", "used": 0, "capacity": 0}])
        self.assertEqual(report["terms"][0]["enrollments"], [])
        self.assertEqual(objective(report)["completion_indices"], [2])

    def test_deferral_across_terms_with_one_unique_seat_per_term(self):
        # Both students individually need A then B across two terms, but every
        # offering has a single shared seat: only one can be scheduled.
        data = base(program={"required_courses": ["A", "B"],
                             "minimum_credits": {"STEM": 6}})
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 3, [("B", 1)])])
        report = plan(data)
        rows = students_by_id(report)
        self.assertEqual(objective(report)["graduates"], 1)
        self.assertEqual(rows["s1"]["status"], "scheduled")
        self.assertEqual(rows["s1"]["graduation_term_index"], 2)
        self.assertEqual(rows["s2"]["status"], "capacity_deferred")
        self.assertTrue(rows["s2"]["individual_graduation_possible"])
        self.assertEqual(objective(report)["completion_indices"], [2, 3])

    def test_unreachable_distinct_from_deferral(self):
        program = {"required_courses": ["C"],
                   "minimum_credits": {"STEM": 10}}
        # C needs A; A is never offered at all.
        data = base(program=program)
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 10, [("B", 2), ("C", 2)])])
        rows = students_by_id(plan(data))
        for sid in ("s1", "s2"):
            self.assertEqual(rows[sid]["status"], "unreachable")
            self.assertFalse(rows[sid]["individual_graduation_possible"])


class AlreadyEligibleAndEmptyTests(unittest.TestCase):
    def test_already_eligible_student_takes_no_seats(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data["attempts"] = [
            {"id": "a1", "student_id": "s1", "course_id": "A",
             "date": "2026-01-01", "mark": 90}]
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)])])
        report = plan(data)
        rows = students_by_id(report)
        self.assertEqual(rows["s1"]["status"], "already_eligible")
        self.assertTrue(rows["s1"]["individual_graduation_possible"])
        self.assertEqual(rows["s1"]["graduation_term_index"], 0)
        self.assertIsNone(rows["s1"]["graduation_term_id"])
        self.assertEqual(rows["s1"]["planned_courses"], [])
        # The single A seat goes to s2.
        self.assertEqual(rows["s2"]["status"], "scheduled")
        self.assertEqual(report["terms"][0]["seat_usage"][0]["used"], 1)

    def test_empty_student_ids_still_emits_terms_and_baseline(self):
        data = base()
        data = with_planning(data, [], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)])])
        report = plan(data)
        self.assertEqual(report["students"], [])
        self.assertEqual(len(report["terms"]), 1)
        self.assertEqual(report["terms"][0]["enrollments"], [])
        self.assertEqual(report["terms"][0]["seat_usage"],
                         [{"course_id": "A", "used": 0, "capacity": 1}])
        self.assertEqual(objective(report), {
            "graduates": 0, "graduation_flags": [], "completion_indices": [],
            "planned_credits": 0, "enrollments": 0})
        self.assertEqual(report["baseline_audit"]["summary"]["students"], 2)

    def test_empty_terms_are_legal_and_no_one_graduates(self):
        data = base()
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [])])
        report = plan(data)
        self.assertEqual(report["terms"][0]["enrollments"], [])
        self.assertEqual(report["terms"][0]["seat_usage"], [])
        row = report["students"][0]
        self.assertEqual(row["status"], "unreachable")
        self.assertFalse(row["individual_graduation_possible"])

    def test_zero_credit_limit_means_no_enrollments(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 0, [("A", 2)])])
        report = plan(data)
        row = report["students"][0]
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(report["terms"][0]["enrollments"], [])


class ObjectiveOrderingTests(unittest.TestCase):
    def test_headcount_dominates_priority_position(self):
        # One shared A seat per term; s2 could take both A and B only if given
        # t1's A, while s1 already holds B. Headcount 2 beats s1 graduating early.
        data = base()
        data["attempts"] = [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}]
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 3, [("A", 1), ("B", 1)])])
        report = plan(data)
        self.assertEqual(objective(report)["graduates"], 2)
        self.assertEqual(objective(report)["completion_indices"], [2, 2])

    def test_flag_lexicographic_prefers_priority_student_on_equal_headcount(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)])])
        report = plan(data)
        self.assertEqual(objective(report)["graduation_flags"], [1, 0])
        # Reversing the registrar's priority order flips who gets the seat.
        data2 = copy.deepcopy(data)
        data2["planning"]["student_ids"] = ["s2", "s1"]
        report2 = plan(data2)
        self.assertEqual(objective(report2)["graduation_flags"], [1, 0])
        self.assertEqual(students_by_id(report2)["s2"]["status"], "scheduled")
        self.assertEqual(students_by_id(report2)["s1"]["status"],
                         "capacity_deferred")

    def test_earliest_graduation_index_wins_on_tied_flags(self):
        # s1 can graduate in t1 or wait to t2; t1 must win.
        data = base(program={"required_courses": ["A", "B"],
                             "minimum_credits": {"STEM": 6}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 6, [("A", 2), ("B", 2)]),
            term("t2", "2027-02-01", "2027-05-31", 6, [("A", 2), ("B", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["graduation_term_index"], 1)
        self.assertEqual(
            [(item["term_id"], item["course_id"]) for item in row["planned_courses"]],
            [("t1", "A"), ("t1", "B")])

    def test_spare_enrollments_are_removed_by_minimum_credit_rule(self):
        # A alone satisfies the program; B is an irrelevant course also offered.
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 10,
             [("A", 2), ("B", 2), ("D", 2)])])
        report = plan(data)
        row = students_by_id(report)["s1"]
        self.assertEqual(row["planned_courses"],
                         [{"term_id": "t1", "course_id": "A"}])
        self.assertEqual(objective(report)["planned_credits"], 3)
        self.assertEqual(objective(report)["enrollments"], 1)

    def test_fewer_enrollment_count_wins_on_credit_tie(self):
        # Add a 3-credit course E equal in credits to A; program wants A only.
        courses = copy.deepcopy(COURSES) + [
            {"id": "E", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []}]
        data = base(courses=courses,
                    program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 10,
             [("A", 2), ("E", 2)])])
        report = plan(data)
        row = students_by_id(report)["s1"]
        self.assertEqual(row["planned_courses"],
                         [{"term_id": "t1", "course_id": "A"}])

    def test_final_tuple_lexicographic_break(self):
        # Two fully equivalent plans (A in t1 vs B in t1, program accepts either
        # as 3 STEM credits with no required course): smallest tuple wins.
        data = base(program={"required_courses": [],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 2), ("B", 2)])])
        report = plan(data)
        row = students_by_id(report)["s1"]
        # (1, 's1', 'A') < (1, 's1', 'B')
        self.assertEqual(row["planned_courses"],
                         [{"term_id": "t1", "course_id": "A"}])


class FutureRecordsTests(unittest.TestCase):
    def test_future_exams_cannot_be_pre_spent(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data["attempts"] = [
            {"id": "future-a", "student_id": "s1", "course_id": "A",
             "date": "2027-01-15", "mark": 99}]
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("B", 2)])])
        report = plan(data)
        row = students_by_id(report)["s1"]
        # The future A pass is only a baseline future record; B does not satisfy
        # the required course A, so graduation is impossible with these offerings.
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(report["baseline_audit"]["students"][0]
                         ["ignored_future_attempts"], 1)

    def test_future_transfer_recognition_is_not_pre_spent_and_plan_is_fixed(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data["transfer"] = {
            "credit_limit": 10,
            "evidence": [
                {"id": "e1", "student_id": "s1", "date": "2026-01-01"}],
            "options": [
                {"id": "future-opt", "student_id": "s1",
                 "evidence_ids": ["e1"], "course_ids": ["A"],
                 "effective_date": "2030-01-01"}],
        }
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("B", 2)])])
        report = plan(data)
        baseline_row = next(r for r in report["baseline_audit"]["students"]
                            if r["student_id"] == "s1")
        # The baseline v2 plan stays empty; planning never re-selects recognition.
        self.assertEqual(baseline_row["recognition_plan"]["option_ids"], [])
        row = students_by_id(report)["s1"]
        self.assertEqual(row["status"], "unreachable")

    def test_baseline_recognition_plan_is_immutable_under_planning(self):
        from tests.test_transfer_validation import with_transfer
        data = with_transfer()
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 10, [("A", 2), ("C", 2)])])
        report = plan(data)
        plain = run(with_transfer())
        baseline_row = next(r for r in report["baseline_audit"]["students"]
                            if r["student_id"] == "s1")
        plain_row = next(r for r in plain["students"] if r["student_id"] == "s1")
        self.assertEqual(baseline_row, plain_row)
        self.assertEqual(report["baseline_audit"], plain)


class PlanningRuleTests(unittest.TestCase):
    def test_completed_course_is_never_rescheduled(self):
        data = base(program={"required_courses": ["A", "B"],
                             "minimum_credits": {"STEM": 6}})
        data["attempts"] = [
            {"id": "a1", "student_id": "s1", "course_id": "A",
             "date": "2026-01-01", "mark": 75}]
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 6, [("A", 2), ("B", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["planned_courses"],
                         [{"term_id": "t1", "course_id": "B"}])
        self.assertEqual(row["projected_completed_courses"], ["A", "B"])
        self.assertEqual(row["projected_credits"], 6)

    def test_projected_gaps_follow_baseline_caliber(self):
        # D is never offered, so graduation is unreachable and the minimum-credit
        # rule schedules nothing: projected gaps stay exactly the baseline gaps.
        data = base(program={"required_courses": ["A", "B", "D"],
                             "minimum_credits": {"STEM": 6, "arts": 2}})
        data["attempts"] = [
            {"id": "a0", "student_id": "s1", "course_id": "A",
             "date": "2026-01-01", "mark": 80}]
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 6, [("A", 2), ("B", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["status"], "unreachable")
        self.assertEqual(row["planned_courses"], [])
        self.assertEqual(row["projected_completed_courses"], ["A"])
        self.assertEqual(row["missing_required"], ["B", "D"])
        self.assertEqual(row["credit_deficits"], {"STEM": 3, "arts": 2})

    def test_scheduled_student_closes_all_gaps_at_graduation(self):
        data = base(program={"required_courses": ["A", "B", "D"],
                             "minimum_credits": {"STEM": 6, "arts": 2}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 8,
             [("A", 2), ("B", 2), ("D", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["missing_required"], [])
        self.assertEqual(row["credit_deficits"], {"STEM": 0, "arts": 0})
        self.assertEqual(row["projected_credits"], 8)

    def test_course_not_offered_cannot_be_taken(self):
        data = base(program={"required_courses": ["D"],
                             "minimum_credits": {"arts": 2}})
        data = with_planning(data, ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 6, [("A", 2)])])
        row = students_by_id(plan(data))["s1"]
        self.assertEqual(row["status"], "unreachable")

    def test_capacity_is_shared_across_all_planning_students(self):
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data["planning"] = {"student_ids": ["s1", "s2"], "terms": [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 2)])]}
        report = plan(data)
        self.assertEqual(objective(report)["graduates"], 2)
        self.assertEqual(report["terms"][0]["seat_usage"][0]["used"], 2)

    def test_individual_possible_uses_full_capacity_alone(self):
        # capacity 1, two students: each alone could graduate, together only one.
        data = base(program={"required_courses": ["A"],
                             "minimum_credits": {"STEM": 3}})
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)])])
        report = plan(data)
        for row in report["students"]:
            self.assertTrue(row["individual_graduation_possible"])

    def test_student_order_is_preserved_in_report(self):
        data = base()
        data = with_planning(data, ["s2", "s1"], [])
        report = plan(data)
        self.assertEqual([row["student_id"] for row in report["students"]],
                         ["s2", "s1"])

    def test_objective_matches_detail_rows(self):
        data = base()
        data["attempts"] = [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}]
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 3, [("A", 1), ("B", 1)])])
        report = plan(data)
        rows = students_by_id(report)
        obj = objective(report)
        self.assertEqual(obj["graduates"],
                         sum(1 for row in report["students"]
                             if row["status"] in ("already_eligible", "scheduled")))
        self.assertEqual(obj["graduation_flags"],
                         [1 if rows[sid]["graduation_term_index"] is not None
                          else 0 for sid in ["s1", "s2"]])
        self.assertEqual(obj["completion_indices"], [
            rows[sid]["graduation_term_index"]
            if rows[sid]["graduation_term_index"] is not None else 3
            for sid in ["s1", "s2"]])
        self.assertEqual(obj["planned_credits"],
                         sum(courses_credits(report)))
        self.assertEqual(obj["enrollments"],
                         sum(len(t["enrollments"]) for t in report["terms"]))


def courses_credits(report):
    credits = {"A": 3, "B": 3, "C": 4, "D": 2}
    for term_row in report["terms"]:
        for enrollment in term_row["enrollments"]:
            yield credits[enrollment["course_id"]]


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            plan(data)

    def test_missing_planning_node_rejected(self):
        self.assert_rejected(base(), "planning")

    def test_planning_node_must_be_object(self):
        data = base()
        data["planning"] = []
        self.assert_rejected(data, "planning must be an object")
        data = base()
        data["planning"] = None
        self.assert_rejected(data, "planning must be an object")

    def test_missing_planning_fields_rejected(self):
        data = base()
        data["planning"] = {"student_ids": []}
        self.assert_rejected(data, "terms")
        data = base()
        data["planning"] = {"terms": []}
        self.assert_rejected(data, "student_ids")

    def test_student_id_rules(self):
        good = with_planning(base(), ["s1"], [])
        plan(good)
        data = with_planning(base(), ["s1", "s1"], [])
        self.assert_rejected(data, "unique")
        data = with_planning(base(), ["ghost"], [])
        self.assert_rejected(data, "unknown planning")
        data = with_planning(base(), ["s1", "s2", "s1"], [])
        self.assert_rejected(data, "unique")
        data = with_planning(base(), [1], [])
        self.assert_rejected(data, "student_ids")
        data = with_planning(base(), ["s1"] * 4, [])
        self.assert_rejected(data, "at most 3")

    def test_more_than_three_terms_rejected(self):
        terms = [term(f"t{i}", f"2027-0{1 + 2 * i}-01", f"2027-0{2 + 2 * i}-01",
                      3, [("A", 1)]) for i in range(4)]
        self.assert_rejected(with_planning(base(), ["s1"], terms), "at most 3")

    def test_term_id_and_date_rules(self):
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-06-01", "2026-07-01", 3, [])])  # start == as_of
        self.assert_rejected(data, "as_of")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-07-02", "2026-07-01", 3, [])])  # end < start
        self.assert_rejected(data, "start <= end")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-7-1", "2026-08-01", 3, [])])  # non-canonical
        self.assert_rejected(data, "YYYY-MM-DD")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-07-01", "2026-08-01", 3, []),
            term("t1", "2026-09-01", "2026-10-01", 3, [])])
        self.assert_rejected(data, "duplicate planning term")

    def test_overlapping_terms_rejected(self):
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, []),
            term("t2", "2026-12-31", "2027-02-01", 3, [])])  # end == next start
        self.assert_rejected(data, "overlap")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, []),
            term("t2", "2027-01-01", "2027-02-01", 3, [])])
        plan(data)  # strict gap of one day is fine

    def test_offering_rules(self):
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("GHOST", 1)])])
        self.assert_rejected(data, "unknown planning offering")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1), ("A", 2)])])
        self.assert_rejected(data, "duplicate planning offering")
        data = base()
        data["planning"] = {"student_ids": ["s1"], "terms": [
            {"id": "t1", "start": "2026-09-01", "end": "2026-12-31",
             "credit_limit": 3, "offerings": ["A"]}]}
        self.assert_rejected(data, "offerings rows")
        data = base()
        data["planning"] = {"student_ids": ["s1"], "terms": [
            {"id": "  ", "start": "2026-09-01", "end": "2026-12-31",
             "credit_limit": 3, "offerings": []}]}
        self.assert_rejected(data, "terms.id")

    def test_boolean_and_float_impersonating_integers(self):
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", True, [("A", 1)])])
        self.assert_rejected(data, "credit_limit")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", -1)])])
        self.assert_rejected(data, "capacity")
        data = with_planning(base(), ["s1"], [
            term("t1", "2026-09-01", "2026-12-31", 3.0, [("A", 1)])])
        self.assert_rejected(data, "credit_limit")

    def test_missing_term_and_offering_fields(self):
        data = base()
        data["planning"] = {"student_ids": ["s1"], "terms": [
            {"id": "t1", "start": "2026-09-01", "end": "2026-12-31",
             "offerings": []}]}
        self.assert_rejected(data, "credit_limit")
        data = base()
        data["planning"] = {"student_ids": ["s1"], "terms": [
            {"id": "t1", "start": "2026-09-01", "end": "2026-12-31",
             "credit_limit": 3, "offerings": [{"course_id": "A"}]}]}
        self.assert_rejected(data, "capacity")

    def test_wrong_container_types_rejected(self):
        data = base()
        data["planning"] = {"student_ids": "s1", "terms": []}
        self.assert_rejected(data, "student_ids")
        data = base()
        data["planning"] = {"student_ids": [], "terms": {}}
        self.assert_rejected(data, "terms")
        data = with_planning(base(), ["s1"], ["not-a-term"])
        self.assert_rejected(data, "terms rows")
        data = with_planning(base(), ["s1"], [
            {"id": "t1", "start": "2026-09-01", "end": "2026-12-31",
             "credit_limit": 3, "offerings": "nope"}])
        self.assert_rejected(data, "offerings")

    def test_distinct_course_and_pair_limits(self):
        courses = copy.deepcopy(COURSES) + [
            {"id": f"X{i}", "credits": 1, "pass_mark": 60,
             "category": "STEM", "prerequisites": []} for i in range(3)]
        data = base(courses=courses)
        data["planning"] = {"student_ids": ["s1"], "terms": [
            term("t1", "2026-09-01", "2026-12-31", 20,
                 [("A", 1), ("B", 1), ("C", 1), ("D", 1),
                  ("X0", 1), ("X1", 1), ("X2", 1)])]}
        self.assert_rejected(data, "at most 6 distinct")
        data = base(courses=courses)
        data["planning"] = {"student_ids": ["s1"], "terms": [
            term("t1", "2026-09-01", "2026-12-31", 20,
                 [("A", 1), ("B", 1), ("C", 1), ("D", 1), ("X0", 1), ("X1", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 20,
                 [("A", 1), ("B", 1), ("C", 1), ("D", 1)])]}
        self.assert_rejected(data, "at most 9 course-term")

    def test_baseline_validation_still_runs_for_unlisted_students(self):
        data = base()
        data["attempts"] = [
            {"id": "bad", "student_id": "ghost", "course_id": "A",
             "date": "2026-01-01", "mark": 70}]
        data = with_planning(data, ["s1"], [])
        self.assert_rejected(data, "unknown attempt")

    def test_future_transfer_structural_errors_still_rejected(self):
        from tests.test_transfer_validation import with_transfer
        data = with_transfer()
        data["transfer"]["options"].append(
            {"id": "future-bad", "student_id": "s1", "evidence_ids": ["ghost"],
             "course_ids": ["A"], "effective_date": "2030-01-01"})
        self.assert_rejected(data, "evidence_ids")


class PermutationStabilityTests(unittest.TestCase):
    def test_input_order_does_not_change_report(self):
        data = base()
        data["attempts"] = [
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}]
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 3, [("A", 1)]),
            term("t2", "2027-02-01", "2027-05-31", 3, [("A", 1), ("B", 1)])])
        first = plan(data)
        shuffled = copy.deepcopy(data)
        shuffled["planning"]["terms"].reverse()
        for term_row in shuffled["planning"]["terms"]:
            term_row["offerings"].reverse()
        second = plan(shuffled)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_group_and_independent_solver_paths_agree(self):
        # When no seat can bind, the general group DP and the independent
        # fast path must derive the identical optimal objective and enrollment
        # list from the same per-student enumeration.
        from registrar.planning import (
            _attach_demand, _enumerate_student, _solve_group,
            _solve_independent, _validate_planning)
        from registrar.model import validate
        from registrar.service import run
        data = base(program={"required_courses": ["A", "B"],
                             "minimum_credits": {"STEM": 6}})
        data = with_planning(data, ["s1", "s2"], [
            term("t1", "2026-09-01", "2026-12-31", 6, [("A", 9), ("B", 9)]),
            term("t2", "2027-02-01", "2027-05-31", 6, [("A", 9), ("B", 9)])])
        cutoff, courses, students, _attempts, program, _transfer = validate(data)
        _ids, terms = _validate_planning(
            data["planning"], cutoff, courses, students)
        initial = {sid: set() for sid in ["s1", "s2"]}
        plans = {sid: _enumerate_student(sid, initial[sid], False, terms,
                                         courses, program)
                 for sid in ["s1", "s2"]}
        pair_keys = sorted((offset, cid)
                           for offset, t in enumerate(terms, start=1)
                           for cid in t["offerings"])
        pair_index = {pair: i for i, pair in enumerate(pair_keys)}
        capacities = tuple(terms[o - 1]["offerings"][c] for o, c in pair_keys)
        for sid in plans:
            _attach_demand(plans[sid], pair_index)
        group = _solve_group(["s1", "s2"], plans, capacities, len(terms))
        independent = _solve_independent(["s1", "s2"], plans, len(terms))
        self.assertEqual(group, independent)
        report = plan(data)
        self.assertEqual(report["objective"]["completion_indices"], [1, 1])


if __name__ == "__main__":
    unittest.main()
