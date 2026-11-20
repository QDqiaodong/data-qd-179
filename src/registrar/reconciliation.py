"""匿名答卷对账与毕业结论核验（credit-reconcile 的服务入口）。

考务处的座位表记录学生、课程与考试日，阅卷处的答卷只有匿名编号与候选链接。
每个阅卷批次（wave）独立对账：在只允许使用给定链接、一张答卷与一个座位各
至多使用一次的前提下，先最大化匹配对数，再在并列方案中最大化 verified
（另有签收记录支持的）链接数，保留全部并列最优匹配；分数、毕业结论与输入
顺序都不参与对账。每个批次各取一个最优匹配才构成一份可能归档，匹配上的
座位按答卷分数生成考试，未匹配座位不产生考试，既有 attempts 原样保留。

每名学生的结论只依赖与其待对账座位有关的批次：实现上按学生对相关批次的
本地投影（该生座位在最优匹配中能拿到的答卷向量）做笛卡尔积，从不构造整届
各批匹配的笛卡尔积；一个不含该生座位的批次不会让该生多审核一次。每份可能
归档都从头重走原 v1/v2 审核（含 v2 整包转学分认定的重新选择），不冻结原先
认定。审核次数因此等于
``1（无待对账座位）或 ∏（相关批次本地投影数）``，每维至多 4（一个座位最多
被 3 张答卷列为候选，加空着），每名学生至多 3 个待对账座位，单生审核次数
恒有上界 4**3=64。
"""
from itertools import product

from . import credits, transfers
from .io import integer, text
from .model import parse_day, validate

MAX_WAVES = 120
MAX_ROWS_PER_WAVE = 6
MAX_RECONCILIATION_SEATS = 20000
MAX_CANDIDATE_SHEETS_PER_SEAT = 3
MAX_SEATS_PER_STUDENT = 3


def _validate_reconciliation(node, courses, students, attempts):
    """完整校验 reconciliation 节点，返回按批次 ID 排序的规范化批次列表。"""
    if not isinstance(node, dict):
        raise ValueError("reconciliation must be an object")
    if "waves" not in node:
        raise ValueError("reconciliation.waves is required")
    waves = node["waves"]
    if not isinstance(waves, list):
        raise ValueError("reconciliation.waves must be an array")
    if len(waves) > MAX_WAVES:
        raise ValueError(f"at most {MAX_WAVES} reconciliation waves")

    wave_ids = set()
    all_seat_ids = set()
    all_sheet_ids = set()
    attempt_ids = {row["id"] for row in attempts}
    candidate_refs = {}
    seats_per_student = {}
    parsed = []
    for wave in waves:
        if not isinstance(wave, dict):
            raise ValueError("reconciliation wave rows must be objects")
        for field in ("id", "seats", "sheets"):
            if field not in wave:
                raise ValueError(f"reconciliation.wave.{field} is required")
        wave_id = text(wave["id"], "reconciliation.wave.id")
        if wave_id in wave_ids:
            raise ValueError(f"duplicate reconciliation wave id: {wave_id}")
        wave_ids.add(wave_id)

        raw_seats = wave["seats"]
        if not isinstance(raw_seats, list):
            raise ValueError("reconciliation.wave.seats must be an array")
        if len(raw_seats) > MAX_ROWS_PER_WAVE:
            raise ValueError(
                f"at most {MAX_ROWS_PER_WAVE} seats per reconciliation wave")
        seats = []
        local_seat_ids = set()
        for seat in raw_seats:
            if not isinstance(seat, dict):
                raise ValueError("reconciliation seat rows must be objects")
            for field in ("id", "student_id", "course_id", "date"):
                if field not in seat:
                    raise ValueError(f"reconciliation.seat.{field} is required")
            seat_id = text(seat["id"], "reconciliation.seat.id")
            if seat_id in all_seat_ids:
                raise ValueError(f"duplicate reconciliation seat id: {seat_id}")
            if seat_id in attempt_ids:
                raise ValueError(
                    "reconciliation seat id must not duplicate a confirmed"
                    f" attempt id: {seat_id}")
            student_id = text(seat["student_id"], "reconciliation.seat.student_id")
            if student_id not in students:
                raise ValueError(f"unknown reconciliation seat student: {student_id}")
            course_id = text(seat["course_id"], "reconciliation.seat.course_id")
            if course_id not in courses:
                raise ValueError(f"unknown reconciliation seat course: {course_id}")
            day = parse_day(seat["date"]).isoformat()
            all_seat_ids.add(seat_id)
            local_seat_ids.add(seat_id)
            candidate_refs[seat_id] = 0
            seats_per_student[student_id] = seats_per_student.get(student_id, 0) + 1
            seats.append({"id": seat_id, "student_id": student_id,
                          "course_id": course_id, "date": day})

        raw_sheets = wave["sheets"]
        if not isinstance(raw_sheets, list):
            raise ValueError("reconciliation.wave.sheets must be an array")
        if len(raw_sheets) > MAX_ROWS_PER_WAVE:
            raise ValueError(
                f"at most {MAX_ROWS_PER_WAVE} sheets per reconciliation wave")
        sheets = []
        for sheet in raw_sheets:
            if not isinstance(sheet, dict):
                raise ValueError("reconciliation sheet rows must be objects")
            for field in ("id", "mark", "candidates"):
                if field not in sheet:
                    raise ValueError(f"reconciliation.sheet.{field} is required")
            sheet_id = text(sheet["id"], "reconciliation.sheet.id")
            if sheet_id in all_sheet_ids:
                raise ValueError(f"duplicate reconciliation sheet id: {sheet_id}")
            all_sheet_ids.add(sheet_id)
            mark = integer(sheet["mark"], "reconciliation.sheet.mark")
            if mark > 100:
                raise ValueError("reconciliation.sheet.mark exceeds 100")
            raw_candidates = sheet["candidates"]
            if not isinstance(raw_candidates, list):
                raise ValueError("reconciliation.sheet.candidates must be an array")
            candidates = []
            linked = set()
            for candidate in raw_candidates:
                if not isinstance(candidate, dict):
                    raise ValueError("reconciliation candidate rows must be objects")
                for field in ("seat_id", "verified"):
                    if field not in candidate:
                        raise ValueError(
                            f"reconciliation.candidate.{field} is required")
                seat_id = text(candidate["seat_id"],
                               "reconciliation.candidate.seat_id")
                if seat_id not in local_seat_ids:
                    # 跨批引用与未知座位在此一并拒绝。
                    raise ValueError(
                        "reconciliation candidate references an unknown seat"
                        f" in wave {wave_id}: {seat_id}")
                if seat_id in linked:
                    raise ValueError(
                        "reconciliation sheet lists the same seat twice: "
                        f"{sheet_id} -> {seat_id}")
                linked.add(seat_id)
                if type(candidate["verified"]) is not bool:
                    raise ValueError(
                        "reconciliation.candidate.verified must be a boolean")
                candidates.append({"seat_id": seat_id,
                                   "verified": candidate["verified"]})
                candidate_refs[seat_id] += 1
            sheets.append({"id": sheet_id, "mark": mark,
                           "candidates": candidates})
        parsed.append({"id": wave_id, "seats": seats, "sheets": sheets})

    if len(attempts) + len(all_seat_ids) > MAX_RECONCILIATION_SEATS:
        raise ValueError(
            f"at most {MAX_RECONCILIATION_SEATS} confirmed attempts and"
            " reconciliation seats combined")
    # 座位与已确认考试合在一起时，(student_id, course_id, date) 不得重复。
    seen_keys = set()
    for row in attempts:
        key = (row["student_id"], row["course_id"], row["date"])
        if key in seen_keys:  # model.validate 已拦，防御性保留
            raise ValueError("duplicate student/course/day attempt")
        seen_keys.add(key)
    for wave in parsed:
        for seat in wave["seats"]:
            key = (seat["student_id"], seat["course_id"], seat["date"])
            if key in seen_keys:
                raise ValueError("duplicate student/course/day across attempts"
                                 f" and reconciliation seats: {key}")
            seen_keys.add(key)
    for seat_id, count in candidate_refs.items():
        if count > MAX_CANDIDATE_SHEETS_PER_SEAT:
            raise ValueError(
                f"reconciliation seat {seat_id} is listed by more than "
                f"{MAX_CANDIDATE_SHEETS_PER_SEAT} sheets")
    for student_id, count in seats_per_student.items():
        if count > MAX_SEATS_PER_STUDENT:
            raise ValueError(
                f"student {student_id} has more than {MAX_SEATS_PER_STUDENT}"
                " reconciliation seats")
    parsed.sort(key=lambda wave: wave["id"])
    return parsed


def _solve_wave(seats, sheets):
    """枚举全部可行匹配并保留并列最优者。

    返回 ``(seat_ids, best)``：``seat_ids`` 为按 ID 排序的座位序列，
    ``best`` 为最优匹配列表，每项是与 ``seat_ids`` 对齐的元组，分量为分到
    该座位的答卷 ID，未匹配为 ``None``。先最大化匹配对数，再最大化 verified
    链接数；分数与输入顺序不参与比较。
    """
    seat_ids = sorted(seat["id"] for seat in seats)
    sheet_ids = sorted(sheet["id"] for sheet in sheets)
    links = {}
    for sheet in sheets:
        links[sheet["id"]] = {c["seat_id"]: c["verified"]
                              for c in sheet["candidates"]}

    def enumerate_matchings():
        assignment = {}

        def dive(index):
            if index == len(sheet_ids):
                yield tuple(assignment.get(seat_id) for seat_id in seat_ids)
                return
            yield from dive(index + 1)  # 该答卷不匹配任何座位
            sheet_id = sheet_ids[index]
            for seat_id in sorted(links[sheet_id]):
                if seat_id not in assignment:
                    assignment[seat_id] = sheet_id
                    yield from dive(index + 1)
                    del assignment[seat_id]

        yield from dive(0)

    best = []
    best_key = None
    for matching in enumerate_matchings():
        matched = sum(1 for value in matching if value is not None)
        verified = sum(
            1 for seat_id, sheet_id in zip(seat_ids, matching)
            if sheet_id is not None and links[sheet_id][seat_id])
        key = (-matched, -verified)
        if best_key is None or key < best_key:
            best_key = key
            best = [matching]
        elif key == best_key:
            best.append(matching)
    return seat_ids, best


def _vector_key(vector):
    """sheet_id 向量的字典序键：None 哨兵排在所有字符串之后。"""
    return tuple((1, "") if value is None else (0, value) for value in vector)


class _Reconciler:
    """承载一次对账的全部中间状态与可复算的审核计数。"""

    def __init__(self, data):
        cutoff, courses, students, attempts, program, transfer = validate(data)
        if "reconciliation" not in data:
            raise ValueError("reconciliation is required")
        waves = _validate_reconciliation(
            data["reconciliation"], courses, students, attempts)
        self.cutoff = cutoff
        self.courses = courses
        self.students = students
        self.attempts = attempts
        self.program = program
        self.transfer = transfer
        self.base_by_student = {sid: [] for sid in students}
        for row in attempts:
            self.base_by_student[row["student_id"]].append(row)
        self.audit_count = 0
        self.audits_by_student = {}
        self.wave_solutions = [self._build_solution(wave) for wave in waves]

    def _build_solution(self, wave):
        seat_ids, best = _solve_wave(wave["seats"], wave["sheets"])
        seat_by_id = {seat["id"]: seat for seat in wave["seats"]}
        sheet_by_id = {sheet["id"]: sheet for sheet in wave["sheets"]}
        verified_pairs = {(c["seat_id"], sheet["id"])
                          for sheet in wave["sheets"]
                          for c in sheet["candidates"] if c["verified"]}
        # 每名学生在此批次中的本地投影：其座位（按座位 ID 排）在任一最优
        # 匹配中能拿到的答卷向量 -> 一个实现该投影的完整匹配代表。
        groups_by_student = {}
        own_seats = {}
        for seat_id in seat_ids:
            student_id = seat_by_id[seat_id]["student_id"]
            own_seats.setdefault(student_id, []).append(seat_id)
        for student_id, own in own_seats.items():
            positions = [seat_ids.index(seat_id) for seat_id in own]
            groups = {}
            for matching in best:
                projection = tuple(matching[pos] for pos in positions)
                incumbent = groups.get(projection)
                # 见证按完整匹配向量取字典序最小；同投影下保留最小的代表。
                if incumbent is None or _vector_key(matching) < _vector_key(incumbent):
                    groups[projection] = matching
            groups_by_student[student_id] = {
                "seat_ids": own, "positions": positions, "groups": groups}
        return {"id": wave["id"], "seats": wave["seats"], "sheets": wave["sheets"],
                "seat_ids": seat_ids, "best": best, "seat_by_id": seat_by_id,
                "sheet_by_id": sheet_by_id, "verified_pairs": verified_pairs,
                "groups_by_student": groups_by_student}

    def _audit(self, student_id, rows):
        self.audit_count += 1
        self.audits_by_student[student_id] = \
            self.audits_by_student.get(student_id, 0) + 1
        if self.transfer is None:
            return credits.student_result(
                student_id, rows, self.courses, self.program, self.cutoff)
        options = self.transfer["by_student"].get(student_id, [])
        return transfers.student_transfer_result(
            student_id, rows, options, self.transfer["credit_limit"],
            self.courses, self.program, self.cutoff)

    def _student_outcome(self, student_id):
        relevant = [solution for solution in self.wave_solutions
                    if student_id in solution["groups_by_student"]]
        base_rows = self.base_by_student.get(student_id, [])
        minimum = maximum = None
        saw_eligible = saw_ineligible = False
        witnesses = {}
        # 各相关批次的本地投影笛卡尔积；无关批次完全不出现，审核次数与
        # 批次数无关，只随该生自己座位所在批次的投影数增长。
        group_axes = []
        for solution in relevant:
            local = solution["groups_by_student"][student_id]
            ordered = sorted(local["groups"].items(),
                             key=lambda item: _vector_key(item[0]))
            group_axes.append((solution, local, ordered))
        for combo in product(*(ordered for _s, _l, ordered in group_axes)):
            extra_rows = []
            flat_vector = []
            for (solution, local, _ordered), (projection, representative) in zip(
                    group_axes, combo):
                flat_vector.extend(representative)
                for seat_id, sheet_id in zip(local["seat_ids"], projection):
                    if sheet_id is not None:
                        seat = solution["seat_by_id"][seat_id]
                        sheet = solution["sheet_by_id"][sheet_id]
                        extra_rows.append({
                            "id": seat_id, "student_id": student_id,
                            "course_id": seat["course_id"], "date": seat["date"],
                            "mark": sheet["mark"]})
            row = self._audit(student_id, base_rows + extra_rows)
            earned = row["credits"]
            minimum = earned if minimum is None else min(minimum, earned)
            maximum = earned if maximum is None else max(maximum, earned)
            eligible = row["graduation_eligible"]
            saw_eligible = saw_eligible or eligible
            saw_ineligible = saw_ineligible or not eligible
            outcome = "eligible" if eligible else "ineligible"
            incumbent = witnesses.get(outcome)
            if incumbent is None or _vector_key(flat_vector) < _vector_key(
                    incumbent[1]):
                assignments = []
                for (solution, _local, _ordered), (_projection, representative) in zip(
                        group_axes, combo):
                    for seat_id, sheet_id in zip(solution["seat_ids"],
                                                 representative):
                        assignments.append({"wave_id": solution["id"],
                                            "seat_id": seat_id,
                                            "sheet_id": sheet_id})
                witnesses[outcome] = (row, flat_vector, assignments)
        if minimum is None:  # 没有待对账座位：只审核原确认考试一次。
            row = self._audit(student_id, base_rows)
            minimum = maximum = row["credits"]
            saw_eligible = row["graduation_eligible"]
            saw_ineligible = not saw_eligible
            witnesses["eligible" if saw_eligible else "ineligible"] = \
                (row, (), [])
        if saw_eligible and saw_ineligible:
            status = "needs_review"
        elif saw_eligible:
            status = "eligible"
        else:
            status = "ineligible"

        def witness_payload(outcome):
            if outcome not in witnesses:
                return None
            row, _vector, assignments = witnesses[outcome]
            return {"audit": row, "assignments": assignments}

        return {
            "student_id": student_id,
            "credit_range": [minimum, maximum],
            "graduation_status": status,
            "witnesses": {
                "eligible": witness_payload("eligible"),
                "ineligible": witness_payload("ineligible"),
            },
        }

    def build_report(self):
        wave_reports = []
        for solution in self.wave_solutions:
            seat_rows = []
            for position, seat_id in enumerate(solution["seat_ids"]):
                possible = sorted({matching[position]
                                   for matching in solution["best"]
                                   if matching[position] is not None})
                may_be_unmatched = any(
                    matching[position] is None for matching in solution["best"])
                seat_rows.append({"seat_id": seat_id,
                                  "possible_sheet_ids": possible,
                                  "may_be_unmatched": may_be_unmatched})
            first = solution["best"][0]
            matched_count = sum(
                1 for value in first if value is not None)
            verified_count = sum(
                1 for seat_id, sheet_id in zip(solution["seat_ids"], first)
                if sheet_id is not None
                and (seat_id, sheet_id) in solution["verified_pairs"])
            wave_reports.append({
                "id": solution["id"], "matched_count": matched_count,
                "verified_count": verified_count, "seats": seat_rows})
        student_rows = [self._student_outcome(student_id)
                        for student_id in sorted(self.students)]
        return {"schema_version": 1, "as_of": self.cutoff.isoformat(),
                "waves": wave_reports, "students": student_rows}


def reconcile(data):
    """对含 reconciliation.waves 的 v1/v2 输入做匿名答卷对账与结论核验。"""
    return _Reconciler(data).build_report()
