"""主辅修联合审核（``registrar.awards.review`` 与 ``credit-awards`` 的服务入口）。

同一份成绩快照既要核对主修毕业，也要核对辅修结业。本模块在不改变任何原
审核事实的前提下，为每名列入学生把已取得学分的课程在主修与各辅修要求组
之间做整门、互斥的分配：

- 先枚举该生截至 ``as_of`` 全部可行的转学分方案组合（v2 规则原样保留，
  空方案也在内），每组方案都从头重走一遍原审核，得到该组合自己的证据；
- 再在这份证据之上枚举全部合法的证书用途分配，主修必修取得学分后必须留
  在主修，其余课程最多整门划给一个列名且来源相符的辅修组；
- 最后按契约的逐级字典序，在“方案 × 分配”的完整候选空间里选一份最优
  结果，而不是拿旧 ``credit-audit`` 选好的方案直接分配。

学分与下限没有小整数上界：所有枚举只按课程/方案的个数进行，从不按学分
数值展开数组或循环。
"""
from . import credits, transfers
from .io import integer, text
from .model import validate

MAX_AWARD_STUDENTS = 12
MAX_GROUPS = 4
MAX_GROUP_COURSES = 10
MAX_GROUP_MEMBERSHIP = 2
MAX_ACTIVE_OPTIONS = 6


def _validate_awards(node, courses, students, cutoff, transfer):
    """完整校验 awards 节点，返回 (学生 ID 列表, 规范化辅修组列表)。"""
    if not isinstance(node, dict):
        raise ValueError("awards must be an object")
    for field in ("student_ids", "groups"):
        if field not in node:
            raise ValueError(f"awards.{field} is required")

    raw_ids = node["student_ids"]
    if not isinstance(raw_ids, list):
        raise ValueError("awards.student_ids must be an array")
    if len(raw_ids) > MAX_AWARD_STUDENTS:
        raise ValueError(f"awards.student_ids allows at most {MAX_AWARD_STUDENTS} students")
    student_ids = []
    seen = set()
    for sid in raw_ids:
        text(sid, "awards.student_ids")
        if sid in seen:
            raise ValueError("awards.student_ids must be unique")
        if sid not in students:
            raise ValueError(f"unknown awards.student_ids reference: {sid}")
        seen.add(sid)
        student_ids.append(sid)

    raw_groups = node["groups"]
    if not isinstance(raw_groups, list) or not 1 <= len(raw_groups) <= MAX_GROUPS:
        raise ValueError(f"awards.groups must be an array of 1..{MAX_GROUPS} rows")
    allowed = {"id", "course_ids", "minimum_credits", "source"}
    group_ids = set()
    membership = {}
    distinct_courses = set()
    groups = []
    for row in raw_groups:
        if not isinstance(row, dict):
            raise ValueError("awards group rows must be objects")
        if set(row) != allowed:
            raise ValueError(
                "awards group rows must contain exactly "
                "id, course_ids, minimum_credits, source")
        gid = text(row["id"], "awards.groups.id")
        if gid in group_ids:
            raise ValueError(f"duplicate awards group id: {gid}")
        group_ids.add(gid)
        course_ids = row["course_ids"]
        if not isinstance(course_ids, list) or not course_ids:
            raise ValueError("awards.groups.course_ids must be a nonempty array")
        if any(not isinstance(cid, str) for cid in course_ids):
            raise ValueError("awards.groups.course_ids must contain strings")
        if len(set(course_ids)) != len(course_ids):
            raise ValueError("awards.groups.course_ids must be unique within a group")
        for cid in course_ids:
            if cid not in courses:
                raise ValueError(f"unknown awards group course reference: {cid}")
            membership[cid] = membership.get(cid, 0) + 1
            distinct_courses.add(cid)
        minimum = integer(row["minimum_credits"],
                          "awards.groups.minimum_credits", 1)
        source = row["source"]
        if source not in ("any", "exam"):
            raise ValueError("awards.groups.source must be any or exam")
        groups.append({"id": gid, "course_ids": list(course_ids),
                       "minimum_credits": minimum, "source": source})
    if len(distinct_courses) > MAX_GROUP_COURSES:
        raise ValueError(
            f"awards groups may reference at most {MAX_GROUP_COURSES} distinct courses")
    for cid, count in membership.items():
        if count > MAX_GROUP_MEMBERSHIP:
            raise ValueError(
                f"course {cid} may appear in at most {MAX_GROUP_MEMBERSHIP} awards groups")

    if transfer is not None:
        # 仅本次列出的学生受小批次核发边界约束；尚未生效的方案仍按原规则
        # （每生最多 12 个）完整校验，不因截至日而豁免结构检查。
        for sid in student_ids:
            active = sum(1 for opt in transfer["by_student"].get(sid, [])
                         if opt["effective_date"] <= cutoff)
            if active > MAX_ACTIVE_OPTIONS:
                raise ValueError(
                    f"student {sid} has more than {MAX_ACTIVE_OPTIONS}"
                    " effective transfer options")
    return student_ids, groups


def _group_eligibility(groups, source_by_course):
    """返回 course_id -> 可接收该课的辅修组索引列表（按组顺序）。"""
    eligible = {}
    for index, group in enumerate(groups):
        for cid in group["course_ids"]:
            if group["source"] == "any" or source_by_course.get(cid) == "exam":
                eligible.setdefault(cid, []).append(index)
    return eligible


def _best_assignment(evidence, groups, courses, program):
    """在一份固定证据上枚举全部合法分配，返回最优 (assignment, 评分)。

    assignment 为 frozenset((group_index, course_id))；取得学分的主修必修
    课不参与枚举，天然留在主修。
    """
    selected = {row["course_id"]: row for row in evidence["selected_passes"]}
    # v1 的已获学分课程均为考试来源；v2 行自带 source 字段。
    source_by_course = {cid: row.get("source", "exam")
                        for cid, row in selected.items()}
    eligible_groups = _group_eligibility(groups, source_by_course)
    required = set(program["required_courses"])
    movable = sorted(cid for cid in selected
                     if cid not in required and cid in eligible_groups)
    minimums = program["minimum_credits"]

    def score(assignment):
        assigned_courses = {cid for _idx, cid in assignment}
        primary = sorted(set(selected) - assigned_courses)
        missing_required = sorted(required - set(primary))
        category_credits = {c["category"]: 0 for c in courses.values()}
        for cid in primary:
            category = courses[cid]["category"]
            category_credits[category] += courses[cid]["credits"]
        primary_deficit = sum(
            max(0, amount - category_credits.get(key, 0))
            for key, amount in minimums.items())
        primary_eligible = not missing_required and primary_deficit == 0

        group_credits = [0] * len(groups)
        for index, cid in assignment:
            group_credits[index] += courses[cid]["credits"]
        deficits = [max(0, groups[i]["minimum_credits"] - group_credits[i])
                    for i in range(len(groups))]
        secondary_eligible = not any(deficits)
        secondary_credits = sum(group_credits)
        return {
            "primary_eligible": primary_eligible,
            "missing_required_count": len(missing_required),
            "primary_deficit": primary_deficit,
            "secondary_eligible": secondary_eligible,
            "secondary_deficit": sum(deficits),
            "secondary_credits": secondary_credits,
            "group_credits": group_credits,
            "deficits": deficits,
        }

    def score_key(assignment, detail):
        # 末级平局规则：排序后的 (组 ID, 课程 ID) 列表字典序最小；
        # 没有分配时为空列表，小于任何非空列表。
        return (
            0 if detail["primary_eligible"] else 1,
            detail["missing_required_count"],
            detail["primary_deficit"],
            0 if detail["secondary_eligible"] else 1,
            detail["secondary_deficit"],
            detail["secondary_credits"],
            _assignment_tail(assignment, groups),
        )

    best = None

    def search(position, used, assignment):
        nonlocal best
        if position == len(movable):
            detail = score(assignment)
            key = score_key(assignment, detail)
            if best is None or key < best[0]:
                best = (key, assignment, detail)
            return
        cid = movable[position]
        search(position + 1, used, assignment)  # 整门课留在主修
        for index in eligible_groups[cid]:
            if index not in used:
                search(position + 1, used | {index},
                       assignment | {(index, cid)})

    search(0, set(), frozenset())
    return best[1], best[2]


def _primary_report(evidence, assignment, courses, program):
    selected = {row["course_id"] for row in evidence["selected_passes"]}
    assigned = {cid for _idx, cid in assignment}
    primary = sorted(selected - assigned)
    category_credits = {c["category"]: 0 for c in courses.values()}
    for cid in primary:
        category_credits[courses[cid]["category"]] += courses[cid]["credits"]
    deficits = {key: max(0, amount - category_credits.get(key, 0))
                for key, amount in sorted(program["minimum_credits"].items())}
    missing_required = sorted(set(program["required_courses"]) - set(primary))
    return {
        "course_ids": primary,
        "credits": sum(courses[cid]["credits"] for cid in primary),
        "credits_by_category": dict(sorted(category_credits.items())),
        "missing_required": missing_required,
        "credit_deficits": deficits,
        "eligible": not missing_required and not any(deficits.values()),
    }


def _secondary_report(assignment, detail, groups, courses):
    rows = []
    for index, group in enumerate(groups):
        course_ids = sorted(cid for idx, cid in assignment if idx == index)
        rows.append({
            "id": group["id"],
            "course_ids": course_ids,
            "credits": detail["group_credits"][index],
            "deficit": detail["deficits"][index],
        })
    rows.sort(key=lambda row: row["id"])
    return {
        "eligible": all(row["deficit"] == 0 for row in rows),
        "credits": detail["secondary_credits"],
        "groups": rows,
    }


def _assignment_tail(assignment, groups):
    """最终平局规则：排序后的 (组 ID, 课程 ID) 列表，Python 字符串顺序。"""
    return sorted((groups[index]["id"], cid) for index, cid in assignment)


def _review_student_v1(sid, rows, groups, courses, program, cutoff):
    # v1 证据固定：唯一候选的排序键就是分配自身的逐级评分（含末级平局规则）。
    evidence = credits.student_result(sid, rows, courses, program, cutoff)
    assignment, detail = _best_assignment(evidence, groups, courses, program)
    return evidence, assignment, detail


def _review_student_v2(sid, rows, options, credit_limit, groups,
                       courses, program, cutoff):
    rows = sorted(rows, key=lambda r: (r["date"], r["id"]))
    available = [opt for opt in options if opt["effective_date"] <= cutoff]
    best = None
    for chosen, charged in transfers.feasible_plans(available, credit_limit, courses):
        # 每组方案都从头审核：同日不解锁、阻断考试不计、重修选分与本校
        # 合法及格优先于转学分的口径全部由原审核函数保证。
        evidence = transfers.build_transfer_student_row(
            sid, rows, chosen, charged, courses, program, cutoff)
        assignment, detail = _best_assignment(evidence, groups, courses, program)
        plan = evidence["recognition_plan"]
        objective = plan["objective"]
        pre_audit_key = (
            0 if objective["graduation_eligible"] else 1,
            -len(set(program["required_courses"])
                 & {p["course_id"] for p in evidence["selected_passes"]}),
            -objective["capped_category_credits"],
            -objective["credits"],
            charged,
            len(chosen),
            plan["option_ids"],
        )
        # 前两级（主修、辅修）由分配评分给出；仍平局时先按 v2 原转学分
        # 排序比较这套方案的分配前审核结果，最后取分配列表字典序最小者。
        key = (
            0 if detail["primary_eligible"] else 1,
            detail["missing_required_count"],
            detail["primary_deficit"],
            0 if detail["secondary_eligible"] else 1,
            detail["secondary_deficit"],
            detail["secondary_credits"],
            pre_audit_key,
            _assignment_tail(assignment, groups),
        )
        if best is None or key < best[0]:
            best = (key, evidence, assignment, detail)
    return best[1], best[2], best[3]


def review(data):
    cutoff, courses, students, attempts, program, transfer = validate(data)
    if "awards" not in data:
        raise ValueError("awards is required")
    student_ids, groups = _validate_awards(
        data["awards"], courses, students, cutoff, transfer)

    grouped = {key: [] for key in students}
    for attempt in attempts:
        grouped[attempt["student_id"]].append(attempt)

    report_rows = []
    for sid in sorted(student_ids):
        rows = grouped[sid]
        if transfer is None:
            evidence, assignment, detail = _review_student_v1(
                sid, rows, groups, courses, program, cutoff)
        else:
            options = transfer["by_student"].get(sid, [])
            evidence, assignment, detail = _review_student_v2(
                sid, rows, options, transfer["credit_limit"],
                groups, courses, program, cutoff)
        report_rows.append({
            "student_id": sid,
            "evidence_audit": evidence,
            "primary": _primary_report(evidence, assignment, courses, program),
            "secondary": _secondary_report(assignment, detail, groups, courses),
        })

    return {"schema_version": 1, "as_of": cutoff.isoformat(),
            "students": report_rows}
