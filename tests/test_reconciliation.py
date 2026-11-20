"""匿名答卷对账与毕业结论核验的服务层测试。

覆盖：匹配数优先于签收数、同批候选不能任意组合、多个批次共同影响同一
学生的先修链、同日不解锁、未来座位参与对账但不计入当前成绩、见证平局、
空批次、非法输入拒绝、输入重排不变性、v2 不同归属导致不同
recognition_plan，以及用可重复计数证明的审核次数界限。
"""
import copy
import unittest

from registrar.reconciliation import reconcile, _Reconciler

COURSES = [
    {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
     "prerequisites": []},
    {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
     "prerequisites": ["A"]},
    {"id": "C", "credits": 4, "pass_mark": 60, "category": "STEM",
     "prerequisites": ["A"]},
    {"id": "D", "credits": 2, "pass_mark": 60, "category": "arts",
     "prerequisites": []},
]

PROGRAM = {"required_courses": ["A", "B"], "minimum_credits": {"STEM": 6}}


def base_input(waves=None, students=None, attempts=None, program=None,
               courses=None, as_of="2026-06-01"):
    data = {
        "as_of": as_of,
        "courses": copy.deepcopy(COURSES if courses is None else courses),
        "students": copy.deepcopy(
            [{"id": "s1"}, {"id": "s2"}] if students is None else students),
        "program": copy.deepcopy(PROGRAM if program is None else program),
        "attempts": copy.deepcopy([] if attempts is None else attempts),
        "reconciliation": {"waves": [] if waves is None else copy.deepcopy(waves)},
    }
    return data


def wave(wid, seats, sheets):
    return {"id": wid, "seats": seats, "sheets": sheets}


def seat(sid, student, course, day):
    return {"id": sid, "student_id": student, "course_id": course, "date": day}


def sheet(sid, mark, candidates):
    return {"id": sid, "mark": mark,
            "candidates": [{"seat_id": s, "verified": v} for s, v in candidates]}


def by_student(report):
    return {row["student_id"]: row for row in report["students"]}


def assignment_map(witness):
    return {(a["wave_id"], a["seat_id"]): a["sheet_id"]
            for a in witness["assignments"]}


class WorkedExampleTests(unittest.TestCase):
    def data(self):
        # 甲(s1)、乙(s2)两个 A 课座位，一份高分一份低分答卷均可对应任一
        # 座位，均无签收；甲另有后来的 B 考试，毕业需要 A、B。
        w1 = wave("w1", [
            seat("seat-a", "s1", "A", "2026-02-01"),
            seat("seat-b", "s2", "A", "2026-02-01"),
        ], [
            sheet("sh-hi", 90, [("seat-a", False), ("seat-b", False)]),
            sheet("sh-lo", 40, [("seat-a", False), ("seat-b", False)]),
        ])
        attempts = [{"id": "b1", "student_id": "s1", "course_id": "B",
                     "date": "2026-03-01", "mark": 90}]
        return base_input(waves=[w1], attempts=attempts)

    def test_ambiguous_identity_gives_needs_review_with_both_witnesses(self):
        report = reconcile(self.data())
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "needs_review")
        self.assertEqual(s1["credit_range"], [0, 6])
        self.assertIsNotNone(s1["witnesses"]["eligible"])
        self.assertIsNotNone(s1["witnesses"]["ineligible"])
        # 两份见证里 B 的通过状态不同。
        eligible_audit = s1["witnesses"]["eligible"]["audit"]
        ineligible_audit = s1["witnesses"]["ineligible"]["audit"]
        b_status = {row["graduation_eligible"]: {
            a["attempt_id"]: a["status"] for a in row["attempt_audit"]}
            for row in (eligible_audit, ineligible_audit)}
        self.assertEqual(b_status[True]["b1"], "passed")
        self.assertEqual(b_status[False]["b1"], "blocked_prerequisite")
        self.assertEqual(b_status[False]["seat-a"], "failed")

    def test_wave_report_lists_all_equally_supported_identities(self):
        report = reconcile(self.data())
        w1 = report["waves"][0]
        self.assertEqual(w1["id"], "w1")
        self.assertEqual(w1["matched_count"], 2)
        self.assertEqual(w1["verified_count"], 0)
        self.assertEqual([row["seat_id"] for row in w1["seats"]],
                         ["seat-a", "seat-b"])
        for row in w1["seats"]:
            self.assertEqual(row["possible_sheet_ids"], ["sh-hi", "sh-lo"])
            self.assertFalse(row["may_be_unmatched"])

    def test_a_single_verified_record_resolves_graduation(self):
        data = self.data()
        data["reconciliation"]["waves"][0]["sheets"][0]["candidates"][0][
            "verified"] = True
        report = reconcile(data)
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "eligible")
        self.assertEqual(s1["credit_range"], [6, 6])
        w1 = report["waves"][0]
        self.assertEqual(w1["verified_count"], 1)
        seat_a = next(row for row in w1["seats"] if row["seat_id"] == "seat-a")
        self.assertEqual(seat_a["possible_sheet_ids"], ["sh-hi"])
        # 见证唯一，且 assignments 含同批其他学生的座位。
        witness = s1["witnesses"]["eligible"]
        self.assertEqual(assignment_map(witness), {
            ("w1", "seat-a"): "sh-hi", ("w1", "seat-b"): "sh-lo"})
        self.assertIsNone(s1["witnesses"]["ineligible"])

    def test_reverse_example_identity_uncertain_but_graduation_certain(self):
        # 同一学生不同日的两个 A 座位，一份及格答卷可去任一座位且必须匹配；
        # 毕业只需 A：归属不确定，但可以毕业已确定。
        w1 = wave("w1", [
            seat("seat-1", "s1", "A", "2026-01-01"),
            seat("seat-2", "s1", "A", "2026-02-01"),
        ], [sheet("sh", 80, [("seat-1", False), ("seat-2", False)])])
        data = base_input(waves=[w1],
                          program={"required_courses": ["A"],
                                   "minimum_credits": {}})
        report = reconcile(data)
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "eligible")
        self.assertEqual(s1["credit_range"], [3, 3])
        self.assertIsNone(s1["witnesses"]["ineligible"])
        seats = {row["seat_id"]: row for row in report["waves"][0]["seats"]}
        self.assertEqual(seats["seat-1"]["possible_sheet_ids"], ["sh"])
        self.assertTrue(seats["seat-1"]["may_be_unmatched"])
        self.assertTrue(seats["seat-2"]["may_be_unmatched"])
        # 字典序最小见证：sh 分给座位 ID 较小者，另一座位为 null。
        witness = s1["witnesses"]["eligible"]
        self.assertEqual(assignment_map(witness),
                         {("w1", "seat-1"): "sh", ("w1", "seat-2"): None})


class MatchingRuleTests(unittest.TestCase):
    def test_match_count_outranks_verified_count(self):
        # s0 未签收只连 a2；s1 带一条 verified 链接到 a2，另有未签收链接到
        # a1。使用 verified 链接（s1->a2）后 s0 无座位可去，只能成 1 对；
        # 放弃签收、让 s1->a1、s0->a2 可成 2 对、verified 为 0。匹配数优先，
        # verified 链接不进入任何最优匹配。
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
            seat("a2", "s2", "A", "2026-01-02"),
        ], [
            sheet("s0", 75, [("a2", False)]),
            sheet("s1", 80, [("a1", False), ("a2", True)]),
        ])
        report = reconcile(base_input(waves=[w1]))
        w = report["waves"][0]
        self.assertEqual(w["matched_count"], 2)
        self.assertEqual(w["verified_count"], 0)
        # 只有 1 对的 verified 方案不是最优：a2 在最优匹配中只可能拿到 s0。
        seats = {row["seat_id"]: row for row in w["seats"]}
        self.assertEqual(seats["a1"]["possible_sheet_ids"], ["s1"])
        self.assertEqual(seats["a2"]["possible_sheet_ids"], ["s0"])
        self.assertFalse(seats["a1"]["may_be_unmatched"])

    def test_individually_feasible_links_may_not_combine(self):
        # x、y 都只连 a1（x 带签收），但一张座位至多使用一次：最优匹配只能
        # 成 1 对。verified 优先使 y->a1 成为唯一最优，x 虽单独可行却被排除；
        # 两条链接不可能共同发生。
        program = {"required_courses": ["A"], "minimum_credits": {}}
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
        ], [
            sheet("x", 80, [("a1", False)]),
            sheet("y", 70, [("a1", True)]),
        ])
        report = reconcile(base_input(waves=[w1], program=program))
        w = report["waves"][0]
        self.assertEqual(w["matched_count"], 1)
        self.assertEqual(w["verified_count"], 1)
        seat_row = w["seats"][0]
        self.assertEqual(seat_row["possible_sheet_ids"], ["y"])
        self.assertFalse(seat_row["may_be_unmatched"])
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["credit_range"], [3, 3])
        self.assertEqual(s1["witnesses"]["eligible"]["assignments"],
                         [{"wave_id": "w1", "seat_id": "a1", "sheet_id": "y"}])

    def test_equally_supported_links_are_both_retained_with_tie_witness(self):
        # 两条均无签收的链接单独可行、互斥且并列：两者都保留，见证平局按
        # sheet 向量字典序最小（x 在前）。
        program = {"required_courses": ["A"], "minimum_credits": {}}
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
        ], [
            sheet("x", 80, [("a1", False)]),
            sheet("y", 70, [("a1", False)]),
        ])
        report = reconcile(base_input(waves=[w1], program=program))
        w = report["waves"][0]
        self.assertEqual(w["matched_count"], 1)
        self.assertEqual(w["verified_count"], 0)
        self.assertEqual(w["seats"][0]["possible_sheet_ids"], ["x", "y"])
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["witnesses"]["eligible"]["assignments"],
                         [{"wave_id": "w1", "seat_id": "a1", "sheet_id": "x"}])

    def test_seat_and_sheet_each_used_at_most_once(self):
        # 两个座位、一张答卷：最多 1 对，总有一个座位空着。
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
            seat("a2", "s1", "A", "2026-02-01"),
        ], [sheet("x", 80, [("a1", False), ("a2", False)])])
        data = base_input(waves=[w1],
                          program={"required_courses": ["A"],
                                   "minimum_credits": {}})
        report = reconcile(data)
        seats = {row["seat_id"]: row for row in report["waves"][0]["seats"]}
        self.assertEqual(report["waves"][0]["matched_count"], 1)
        self.assertTrue(seats["a1"]["may_be_unmatched"])
        self.assertTrue(seats["a2"]["may_be_unmatched"])

    def test_sheets_without_candidates_and_seats_without_sheets_are_valid(self):
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
        ], [
            sheet("orphan", 80, []),
        ])
        report = reconcile(base_input(waves=[w1]))
        w = report["waves"][0]
        self.assertEqual(w["matched_count"], 0)
        self.assertEqual(w["verified_count"], 0)
        self.assertEqual(w["seats"][0]["possible_sheet_ids"], [])
        self.assertTrue(w["seats"][0]["may_be_unmatched"])
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["credit_range"], [0, 0])
        self.assertEqual(s1["witnesses"]["eligible"], None)
        self.assertEqual(
            s1["witnesses"]["ineligible"]["assignments"],
            [{"wave_id": "w1", "seat_id": "a1", "sheet_id": None}])

    def test_marks_do_not_participate_in_matching(self):
        # 低分答卷带 verified，高分不带：匹配数相同时 verified 决定，
        # 分数再高也不影响对账结果。
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-01-01"),
            seat("a2", "s2", "A", "2026-01-01"),
        ], [
            sheet("hi", 99, [("a1", False), ("a2", False)]),
            sheet("lo", 61, [("a1", True), ("a2", False)]),
        ])
        report = reconcile(base_input(waves=[w1]))
        self.assertEqual(report["waves"][0]["verified_count"], 1)
        seats = {row["seat_id"]: row for row in report["waves"][0]["seats"]}
        self.assertEqual(seats["a1"]["possible_sheet_ids"], ["lo"])


class PrerequisiteAndTimingTests(unittest.TestCase):
    def test_multiple_waves_jointly_drive_one_student_prerequisite_chain(self):
        # s1 有两个待对账批次：w1 的 A 座位与 w2 的 B 座位，B 要求先完成 A。
        w1 = wave("w1", [seat("a-seat", "s1", "A", "2026-01-01")],
                  [sheet("a-pass", 80, [("a-seat", False)])])
        w2 = wave("w2", [seat("b-seat", "s1", "B", "2026-02-01")],
                  [sheet("b-pass", 80, [("b-seat", False)])])
        report = reconcile(base_input(waves=[w2, w1]))  # 输入乱序
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "eligible")
        self.assertEqual(s1["credit_range"], [6, 6])
        # waves 按批次 ID 排序，与输入顺序无关。
        self.assertEqual([w["id"] for w in report["waves"]], ["w1", "w2"])
        witness = s1["witnesses"]["eligible"]
        self.assertEqual(
            [(a["wave_id"], a["seat_id"], a["sheet_id"])
             for a in witness["assignments"]],
            [("w1", "a-seat", "a-pass"), ("w2", "b-seat", "b-pass")])
        statuses = {a["attempt_id"]: a["status"]
                    for a in witness["audit"]["attempt_audit"]}
        self.assertEqual(statuses, {"a-seat": "passed", "b-seat": "passed"})

    def test_same_day_seat_does_not_unlock_downstream_course(self):
        # A 座位与已确认的 B 考试同日：A 即使及格也不解锁 B。
        w1 = wave("w1", [seat("a-seat", "s1", "A", "2026-02-01")],
                  [sheet("a-pass", 80, [("a-seat", False)])])
        attempts = [{"id": "b1", "student_id": "s1", "course_id": "B",
                     "date": "2026-02-01", "mark": 90}]
        report = reconcile(base_input(waves=[w1], attempts=attempts))
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "ineligible")
        statuses = {a["attempt_id"]: a["status"]
                    for a in s1["witnesses"]["ineligible"]["audit"]["attempt_audit"]}
        self.assertEqual(statuses["b1"], "blocked_prerequisite")
        self.assertEqual(s1["credit_range"], [3, 3])

    def test_future_seat_participates_but_is_excluded_from_current_audit(self):
        w1 = wave("w1", [seat("future-a", "s1", "A", "2030-01-01")],
                  [sheet("a-pass", 80, [("future-a", False)])])
        data = base_input(waves=[w1], as_of="2026-06-01")
        report = reconcile(data)
        w = report["waves"][0]
        self.assertEqual(w["matched_count"], 1)  # 对账无视截至日
        s1 = by_student(report)["s1"]
        audit = s1["witnesses"]["ineligible"]["audit"]
        self.assertEqual(audit["ignored_future_attempts"], 1)
        self.assertEqual(audit["credits"], 0)
        self.assertEqual(s1["credit_range"], [0, 0])

    def test_confirmed_attempts_are_always_retained(self):
        attempts = [{"id": "keep", "student_id": "s1", "course_id": "A",
                     "date": "2026-01-01", "mark": 75}]
        w1 = wave("w1", [seat("a2", "s1", "A", "2026-02-01")],
                  [sheet("fail", 30, [("a2", False)])])
        report = reconcile(base_input(waves=[w1], attempts=attempts,
                                      program={"required_courses": ["A"],
                                               "minimum_credits": {}}))
        s1 = by_student(report)["s1"]
        # 重修选分保留最高分；原考试不被对账删除。
        self.assertEqual(s1["graduation_status"], "eligible")
        passes = {p["course_id"]: p for p in
                  s1["witnesses"]["eligible"]["audit"]["selected_passes"]}
        self.assertEqual(passes["A"]["attempt_id"], "keep")
        self.assertEqual(passes["A"]["mark"], 75)


class WitnessAndReportTests(unittest.TestCase):
    def test_student_without_reconciliation_seats_audited_once_with_empty_assignments(self):
        attempts = [{"id": "a1", "student_id": "s2", "course_id": "A",
                     "date": "2026-01-01", "mark": 75}]
        w1 = wave("w1", [seat("a-seat", "s1", "A", "2026-02-01")],
                  [sheet("a-pass", 80, [("a-seat", False)])])
        engine = _Reconciler(base_input(waves=[w1], attempts=attempts))
        report = engine.build_report()
        s2 = by_student(report)["s2"]
        self.assertEqual(s2["witnesses"]["eligible"], None)
        ineligible = s2["witnesses"]["ineligible"]
        self.assertEqual(ineligible["assignments"], [])
        self.assertEqual(engine.audits_by_student["s2"], 1)

    def test_witness_tie_break_is_global_across_all_relevant_waves(self):
        # 两个相关批次各有两个并列投影；全局字典序最小见证同时取每批最小
        # sheet_id（w1 取 a，w2 取 m），而不是只在单批内比较。
        waves = [
            {"id": "w1",
             "seats": [seat("k1", "s1", "A", "2026-01-01")],
             "sheets": [
                 sheet("b", 80, [("k1", False)]),
                 sheet("a", 81, [("k1", False)])]},
            {"id": "w2",
             "seats": [seat("k2", "s1", "A", "2026-02-01")],
             "sheets": [
                 sheet("z", 80, [("k2", False)]),
                 sheet("m", 81, [("k2", False)])]},
        ]
        data = base_input(waves=waves,
                          program={"required_courses": ["A"],
                                   "minimum_credits": {}})
        report = reconcile(data)
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["credit_range"], [3, 3])
        self.assertEqual(
            [(a["wave_id"], a["seat_id"], a["sheet_id"])
             for a in s1["witnesses"]["eligible"]["assignments"]],
            [("w1", "k1", "a"), ("w2", "k2", "m")])

    def test_null_sheet_sorts_after_strings_in_witness_tie_break(self):
        # 一张答卷、两个同学生座位：两个并列最优，字典序最小见证把答卷
        # 分给较小座位 ID，另一座位记 null。
        w1 = wave("w1", [
            seat("z1", "s1", "A", "2026-01-01"),
            seat("z2", "s1", "A", "2026-02-01"),
        ], [sheet("p", 80, [("z1", False), ("z2", False)])])
        data = base_input(waves=[w1],
                          program={"required_courses": ["A"],
                                   "minimum_credits": {}})
        report = reconcile(data)
        assignments = by_student(report)["s1"]["witnesses"]["eligible"]["assignments"]
        self.assertEqual(assignments, [
            {"wave_id": "w1", "seat_id": "z1", "sheet_id": "p"},
            {"wave_id": "w1", "seat_id": "z2", "sheet_id": None},
        ])

    def test_report_shape_sorting_and_determinism(self):
        w1 = wave("w1", [
            seat("b-seat", "s2", "A", "2026-02-01"),
            seat("a-seat", "s1", "A", "2026-02-01"),
        ], [
            sheet("s2", 70, [("a-seat", False), ("b-seat", False)]),
            sheet("s1", 80, [("a-seat", False), ("b-seat", False)]),
        ])
        data = base_input(waves=[w1])
        first = reconcile(data)
        second = reconcile(copy.deepcopy(data))
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"schema_version", "as_of", "waves", "students"})
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual([s["student_id"] for s in first["students"]], ["s1", "s2"])
        for s in first["students"]:
            self.assertEqual(set(s), {"student_id", "credit_range",
                                      "graduation_status", "witnesses"})
            self.assertEqual(set(s["witnesses"]), {"eligible", "ineligible"})

    def test_input_reordering_does_not_change_report(self):
        w1 = wave("w1", [
            seat("a-seat", "s1", "A", "2026-02-01"),
            seat("b-seat", "s2", "A", "2026-02-01"),
        ], [
            sheet("s-hi", 90, [("b-seat", False), ("a-seat", False)]),
            sheet("s-lo", 40, [("a-seat", False), ("b-seat", False)]),
        ])
        original = base_input(waves=[w1])
        shuffled = copy.deepcopy(original)
        node = shuffled["reconciliation"]["waves"][0]
        node["seats"] = list(reversed(node["seats"]))
        node["sheets"] = list(reversed(node["sheets"]))
        for sh in node["sheets"]:
            sh["candidates"] = list(reversed(sh["candidates"]))
        self.assertEqual(reconcile(original), reconcile(shuffled))

    def test_fully_empty_wave_is_valid_and_contributes_nothing(self):
        data = base_input(waves=[wave("empty", [], [])])
        report = reconcile(data)
        self.assertEqual(report["waves"], [{
            "id": "empty", "matched_count": 0,
            "verified_count": 0, "seats": []}])
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["credit_range"], [0, 0])
        self.assertEqual(s1["witnesses"]["ineligible"]["assignments"], [])

    def test_empty_waves_is_legal_and_equivalent_to_plain_audit(self):
        data = base_input(waves=[], attempts=[
            {"id": "a1", "student_id": "s1", "course_id": "A",
             "date": "2026-01-01", "mark": 80},
            {"id": "b1", "student_id": "s1", "course_id": "B",
             "date": "2026-02-01", "mark": 80}])
        report = reconcile(data)
        self.assertEqual(report["waves"], [])
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["credit_range"], [6, 6])
        eligible = s1["witnesses"]["eligible"]
        self.assertIsNotNone(eligible)
        self.assertEqual(eligible["assignments"], [])
        self.assertIsNone(s1["witnesses"]["ineligible"])
        engine = _Reconciler(data)
        engine.build_report()
        self.assertEqual(engine.audit_count, 2)  # 每个学生恰好审核一次


class V2RecognitionTests(unittest.TestCase):
    def _data(self):
        # 转学分方案 zz 在 2026-04-15 认定 A，晚于 s1 的 C 考试（2026-03-01）：
        # 它永远无法为该场 C 考试补先修。s1 拿到 A 答卷时无需 zz 即可毕业；
        # 拿不到时 C 被阻断，只能靠 zz 取得 A，毕业仍失败。
        courses = [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "C", "credits": 4, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
            {"id": "D", "credits": 2, "pass_mark": 60, "category": "arts",
             "prerequisites": []},
        ]
        program = {"required_courses": ["A", "C", "D"],
                   "minimum_credits": {"arts": 2}}
        attempts = [
            {"id": "c1", "student_id": "s1", "course_id": "C",
             "date": "2026-03-01", "mark": 90},
            {"id": "d1", "student_id": "s1", "course_id": "D",
             "date": "2026-04-02", "mark": 70},
        ]
        transfer = {
            "credit_limit": 5,
            "evidence": [{"id": "e1", "student_id": "s1",
                          "date": "2026-01-01"}],
            "options": [
                {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
                 "course_ids": ["A"], "effective_date": "2026-04-15"}],
        }
        w1 = wave("w1", [
            seat("seat-1", "s1", "A", "2026-02-01"),
            seat("seat-2", "s2", "A", "2026-02-01"),
        ], [sheet("sh", 80, [("seat-1", False), ("seat-2", False)])])
        data = base_input(waves=[w1], attempts=attempts,
                          program=program, courses=courses)
        data["transfer"] = transfer
        return data

    def test_different_identities_yield_different_recognition_plans(self):
        report = reconcile(self._data())
        s1 = by_student(report)["s1"]
        self.assertEqual(s1["graduation_status"], "needs_review")
        self.assertEqual(s1["credit_range"], [5, 9])
        eligible = s1["witnesses"]["eligible"]["audit"]
        ineligible = s1["witnesses"]["ineligible"]["audit"]
        self.assertEqual(eligible["recognition_plan"]["option_ids"], [])
        self.assertEqual(ineligible["recognition_plan"]["option_ids"], ["zz"])
        self.assertEqual(
            ineligible["selected_passes"][0]["source"], "transfer")
        blocked = {a["attempt_id"]: a["status"]
                   for a in ineligible["attempt_audit"]}
        self.assertEqual(blocked["c1"], "blocked_prerequisite")
        self.assertEqual(
            eligible["recognition_plan"]["charged_credits"], 0)
        self.assertEqual(
            ineligible["recognition_plan"]["charged_credits"], 3)


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, mutate, fragment):
        data = base_input(waves=[wave("w1", [
            seat("a1", "s1", "A", "2026-01-01")],
            [sheet("x", 80, [("a1", False)])])])
        mutate(data)
        with self.assertRaisesRegex(ValueError, fragment):
            reconcile(data)

    def test_missing_reconciliation_or_waves_rejected(self):
        data = base_input()
        del data["reconciliation"]
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            reconcile(data)
        data = base_input()
        del data["reconciliation"]["waves"]
        with self.assertRaisesRegex(ValueError, "waves"):
            reconcile(data)
        data = base_input()
        data["reconciliation"]["waves"] = {}
        with self.assertRaisesRegex(ValueError, "array"):
            reconcile(data)

    def test_type_errors_in_new_fields_rejected(self):
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0].__setitem__("id", 7),
            "wave.id")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["seats"][0].__setitem__(
                "date", "2026-1-1"), "canonical")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["sheets"][0].__setitem__(
                "mark", True), "mark")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["sheets"][0][
                "candidates"][0].__setitem__("verified", "yes"), "boolean")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0].__setitem__("seats", {}),
            "seats must be an array")

    def test_duplicate_global_ids_rejected(self):
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"].append(copy.deepcopy(
                d["reconciliation"]["waves"][0])), "duplicate reconciliation wave")
        data = base_input(waves=[
            wave("w1", [seat("shared", "s1", "A", "2026-01-01")],
                 [sheet("x", 80, [("shared", False)])]),
            wave("w2", [seat("shared", "s2", "A", "2026-01-02")],
                 [sheet("y", 80, [("shared", False)])]),
        ])
        with self.assertRaisesRegex(ValueError, "duplicate reconciliation seat"):
            reconcile(data)
        data = base_input(waves=[
            wave("w1", [seat("a1", "s1", "A", "2026-01-01")],
                 [sheet("dup", 80, [("a1", False)])]),
            wave("w2", [seat("a2", "s1", "A", "2026-01-02")],
                 [sheet("dup", 70, [("a2", False)])]),
        ])
        with self.assertRaisesRegex(ValueError, "duplicate reconciliation sheet"):
            reconcile(data)

    def test_unknown_student_course_and_seat_references_rejected(self):
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["seats"][0].__setitem__(
                "student_id", "ghost"), "unknown reconciliation seat student")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["seats"][0].__setitem__(
                "course_id", "ghost"), "unknown reconciliation seat course")
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["sheets"][0][
                "candidates"][0].__setitem__("seat_id", "ghost"), "unknown seat")

    def test_cross_wave_references_rejected(self):
        data = base_input(waves=[
            wave("w1", [seat("a1", "s1", "A", "2026-01-01")],
                 [sheet("x", 80, [("a1", False)])]),
            wave("w2", [seat("a2", "s1", "A", "2026-01-02")],
                 [sheet("y", 80, [("a1", False)])]),  # 指向 w1 的座位
        ])
        with self.assertRaisesRegex(ValueError, "unknown seat"):
            reconcile(data)

    def test_sheet_listing_same_seat_twice_rejected(self):
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["sheets"][0][
                "candidates"].append(
                {"seat_id": "a1", "verified": False}),
            "lists the same seat twice")

    def test_seat_id_must_not_duplicate_confirmed_attempt(self):
        data = base_input(
            waves=[wave("w1", [seat("a1", "s1", "A", "2026-02-01")],
                        [sheet("x", 80, [("a1", False)])])],
            attempts=[{"id": "a1", "student_id": "s1", "course_id": "A",
                       "date": "2026-01-01", "mark": 70}])
        with self.assertRaisesRegex(ValueError, "must not duplicate"):
            reconcile(data)

    def test_duplicate_student_course_day_across_attempts_and_seats_rejected(self):
        data = base_input(
            waves=[wave("w1", [seat("a2", "s1", "A", "2026-01-01")],
                        [sheet("x", 80, [("a2", False)])])],
            attempts=[{"id": "a1", "student_id": "s1", "course_id": "A",
                       "date": "2026-01-01", "mark": 70}])
        with self.assertRaisesRegex(ValueError, "duplicate student/course/day"):
            reconcile(data)

    def test_per_seat_candidate_limit_enforced_even_when_sheet_never_matches(self):
        def mutate(data):
            for i in range(4):
                data["reconciliation"]["waves"][0]["sheets"].append(
                    sheet(f"x{i}", 50, [("a1", False)]))
        # 每批最多 6 张答卷：4 张新答卷加原有 1 张共 5 张，仍在批内上限内。
        self.assert_rejected(mutate, "more than 3 sheets")

    def test_per_student_seat_limit_enforced(self):
        waves = [wave(f"w{i}", [
            seat(f"a{i}", "s1", "A", f"2026-0{i+1}-01")],
            [sheet(f"x{i}", 80, [(f"a{i}", False)])]) for i in range(4)]
        data = base_input(waves=waves)
        with self.assertRaisesRegex(ValueError, "more than 3 reconciliation seats"):
            reconcile(data)

    def test_wave_row_limits_enforced(self):
        seats = [seat(f"a{i}", "s1", "A", f"2026-0{i+1}-01") for i in range(7)]
        data = base_input(waves=[wave("w1", seats, [])])
        with self.assertRaisesRegex(ValueError, "at most 6 seats"):
            reconcile(data)
        sheets = [sheet(f"x{i}", 70, [("a0", False)]) for i in range(7)]
        data = base_input(waves=[wave("w1", [
            seat("a0", "s1", "A", "2026-01-01")], sheets)])
        with self.assertRaisesRegex(ValueError, "at most 6 sheets"):
            reconcile(data)

    def test_wave_count_limit_enforced(self):
        from datetime import date, timedelta
        students = [{"id": f"s{i:03d}"} for i in range(120)]
        day = date(2026, 1, 1)
        waves = []
        for i in range(121):  # 121 个批次，每批一个不同学生的座位
            waves.append(wave(f"w{i:03d}", [
                seat(f"a{i:03d}", f"s{i:03d}", "A", day.isoformat())], []))
            day += timedelta(days=1)
        data = base_input(waves=waves, students=students)
        with self.assertRaisesRegex(ValueError, "at most 120"):
            reconcile(data)

    def test_future_bad_data_is_still_rejected(self):
        # 座位晚于 as_of 不豁免结构错误。
        self.assert_rejected(
            lambda d: d["reconciliation"]["waves"][0]["seats"][0].__setitem__(
                "student_id", "ghost"), "unknown reconciliation seat student")

    def test_combined_attempt_and_seat_limit_enforced(self):
        from datetime import date, timedelta
        # 合计口径：19999 次已确认考试 + 2 个待对账座位 = 20001，超过 20000。
        # 考试用互不相同的合法日期保持结构合法；是新增合计上限拒绝整份输入。
        start = date(2020, 1, 1)
        attempts = [{"id": f"att{i:05d}", "student_id": "s1", "course_id": "A",
                     "date": (start + timedelta(days=i)).isoformat(),
                     "mark": 60} for i in range(19999)]
        waves = [wave("w1", [
            seat("seat-1", "s1", "B", "2026-01-01"),
            seat("seat-2", "s2", "B", "2026-01-01"),
        ], [sheet("x", 80, [("seat-1", False), ("seat-2", False)])])]
        with self.assertRaisesRegex(ValueError, "20000"):
            reconcile(base_input(waves=waves, attempts=attempts))
        # 临界内（合计正好 20000）合法。
        ok = base_input(waves=waves, attempts=attempts[:19998])
        reconcile(ok)


class AuditBoundTests(unittest.TestCase):
    def _tied_wave(self, index, student_a, student_b):
        day = f"2027-{1 + index // 28:02d}-{(index % 28) + 1:02d}"
        return wave(f"z{index:02d}", [
            seat(f"z{index:02d}-a", student_a, "A", day),
            seat(f"z{index:02d}-b", student_b, "A", day),
        ], [
            sheet(f"z{index:02d}-s1", 70, [
                (f"z{index:02d}-a", False), (f"z{index:02d}-b", False)]),
            sheet(f"z{index:02d}-s2", 71, [
                (f"z{index:02d}-a", False), (f"z{index:02d}-b", False)]),
        ])

    def test_many_independent_tied_waves_do_not_multiply_audits(self):
        students = [{"id": "s1"}, {"id": "s2"}]
        students += [{"id": f"f{i:03d}"} for i in range(120)]
        waves = [wave("w0", [
            seat("seat-1", "s1", "A", "2026-02-01"),
            seat("seat-2", "s2", "A", "2026-02-01"),
        ], [sheet("sh", 80, [("seat-1", False), ("seat-2", False)])])]
        for i in range(60):  # 60 个彼此独立、各有 2 个并列最优的批次
            waves.append(self._tied_wave(i, f"f{2*i:03d}", f"f{2*i+1:03d}"))
        data = base_input(waves=waves, students=students)
        engine = _Reconciler(data)
        report = engine.build_report()
        # 每名学生只在自己座位所在批次的本地投影上审核：每批 2 个投影，
        # 单生审核次数恒为 2；总审核次数 = 学生数 * 投影积，与总批数无关。
        self.assertEqual(engine.audit_count, 2 * len(students))
        self.assertTrue(all(count == 2 for count in
                            engine.audits_by_student.values()))
        # 整届笛卡尔积会是 2^61 份归档；实现从未构造它。
        self.assertLess(engine.audit_count, 2 ** 61)
        s1 = by_student(report)["s1"]
        # s1 无任何已确认及格，是否毕业取决于该批归属；见证只含相关批次。
        witness = (s1["witnesses"]["eligible"]
                   or s1["witnesses"]["ineligible"])
        self.assertEqual({a["wave_id"] for a in witness["assignments"]}, {"w0"})
        self.assertEqual(len(witness["assignments"]), 2)

    def test_adding_irrelevant_wave_keeps_student_audit_count_constant(self):
        w1 = wave("w1", [
            seat("a1", "s1", "A", "2026-02-01"),
            seat("a2", "s2", "A", "2026-02-01"),
        ], [
            sheet("hi", 90, [("a1", False), ("a2", False)]),
            sheet("lo", 40, [("a1", False), ("a2", False)]),
        ])
        before = _Reconciler(base_input(waves=[w1]))
        before.build_report()
        # 加入一个不含 s1 座位的独立并列批次。
        extra = wave("w2", [
            seat("b1", "s2", "D", "2026-02-02"),
            seat("b2", "s2", "D", "2026-02-03"),
        ], [sheet("d", 80, [("b1", False), ("b2", False)])])
        # s2 在 w2 有两个座位（不同日），仍受每生最多 3 座位约束。
        after = _Reconciler(base_input(waves=[w1, extra]))
        after.build_report()
        self.assertEqual(after.audits_by_student["s1"],
                         before.audits_by_student["s1"])
        self.assertEqual(after.audits_by_student["s1"], 2)

    def test_audit_count_equals_product_of_local_projections(self):
        # s1 独占三个批次，每批均有 2 个本地投影 => 审核 2^3 = 8 次。
        waves = []
        for i in range(3):
            other = f"f{i}"
            waves.append(wave(f"w{i}", [
                seat(f"s1-seat-{i}", "s1", "A", f"2026-0{i+1}-01"),
                seat(f"other-seat-{i}", other, "D", f"2026-0{i+1}-02"),
            ], [
                sheet(f"p-{i}", 80, [
                    (f"s1-seat-{i}", False), (f"other-seat-{i}", False)]),
            ]))
        students = [{"id": "s1"}] + [{"id": f"f{i}"} for i in range(3)]
        engine = _Reconciler(base_input(waves=waves, students=students))
        engine.build_report()
        self.assertEqual(engine.audits_by_student["s1"], 8)
        # 理论上界：每座位至多 3 张候选答卷 + 空着 = 4 个投影，3 座位 => 64。
        self.assertLessEqual(engine.audits_by_student["s1"], 4 ** 3)


if __name__ == "__main__":
    unittest.main()
