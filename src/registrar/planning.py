"""小班群体毕业修读计划（credit-plan）。

规划在不可变的当前审核结果（``registrar.service.run`` 的 v1/v2 完整规则）
之上进行：截至日 ``selected_passes`` 是每名学生的起点，未来考试与未来认定
都不能预支。求解器在跨学期先修、个人学分负荷与共享班额约束下，枚举全体
计划学生的同时可履行安排，并按契约规定的五级字典序取精确最优；不使用
贪心，也不拼接独立学生计划。
"""
from . import service
from .io import integer, text
from .model import parse_day, validate


def _validate_planning(node, cutoff, courses, students):
    """校验 planning 节点并返回按开始日排序的学期与有业务含义的学生顺序。"""
    if not isinstance(node, dict):
        raise ValueError("planning must be an object")
    for field in ("student_ids", "terms"):
        if field not in node:
            raise ValueError(f"planning.{field} is required")

    raw_ids = node["student_ids"]
    if not isinstance(raw_ids, list):
        raise ValueError("planning.student_ids must be an array")
    if len(raw_ids) > 3:
        raise ValueError("planning.student_ids allows at most 3 students")
    student_ids = []
    seen = set()
    for sid in raw_ids:
        text(sid, "planning.student_ids")
        if sid in seen:
            raise ValueError("planning.student_ids must be unique")
        if sid not in students:
            raise ValueError(f"unknown planning.student_ids reference: {sid}")
        seen.add(sid)
        student_ids.append(sid)

    raw_terms = node["terms"]
    if not isinstance(raw_terms, list):
        raise ValueError("planning.terms must be an array")
    if len(raw_terms) > 3:
        raise ValueError("planning.terms allows at most 3 terms")
    term_ids = set()
    terms = []
    distinct_courses = set()
    pairs = set()
    for term in raw_terms:
        if not isinstance(term, dict):
            raise ValueError("planning.terms rows must be objects")
        for field in ("id", "start", "end", "credit_limit", "offerings"):
            if field not in term:
                raise ValueError(f"planning.terms.{field} is required")
        tid = text(term["id"], "planning.terms.id")
        if tid in term_ids:
            raise ValueError(f"duplicate planning term id: {tid}")
        term_ids.add(tid)
        start = parse_day(term["start"])
        end = parse_day(term["end"])
        if not (cutoff < start <= end):
            raise ValueError("planning term must satisfy as_of < start <= end")
        limit = integer(term["credit_limit"], "planning.terms.credit_limit", 0)
        raw_offerings = term["offerings"]
        if not isinstance(raw_offerings, list):
            raise ValueError("planning.terms.offerings must be an array")
        offerings = {}
        for off in raw_offerings:
            if not isinstance(off, dict):
                raise ValueError("planning.offerings rows must be objects")
            for field in ("course_id", "capacity"):
                if field not in off:
                    raise ValueError(f"planning.offering.{field} is required")
            cid = text(off["course_id"], "planning.offering.course_id")
            if cid not in courses:
                raise ValueError(f"unknown planning offering course: {cid}")
            if cid in offerings:
                raise ValueError(f"duplicate planning offering in term {tid}: {cid}")
            capacity = integer(off["capacity"], "planning.offering.capacity", 0)
            offerings[cid] = capacity
            distinct_courses.add(cid)
            pairs.add((tid, cid))
        terms.append({"id": tid, "start": start, "end": end,
                      "credit_limit": limit, "offerings": offerings})
    if len(distinct_courses) > 6:
        raise ValueError("planning allows at most 6 distinct offered courses")
    if len(pairs) > 9:
        raise ValueError("planning allows at most 9 course-term pairs")

    ordered = sorted(terms, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if not (previous["end"] < current["start"]):
            raise ValueError("planning terms must not overlap: previous end < next start")
    return student_ids, ordered


def _graduation(completed, program, courses):
    """按原培养方案口径返回 (missing_required, credit_deficits, eligible)。"""
    missing = sorted(set(program["required_courses"]) - completed)
    category_credits = {}
    for cid in completed:
        category = courses[cid]["category"]
        category_credits[category] = category_credits.get(category, 0) \
            + courses[cid]["credits"]
    deficits = {
        key: max(0, amount - category_credits.get(key, 0))
        for key, amount in sorted(program["minimum_credits"].items())}
    eligible = not missing and not any(deficits.values())
    return missing, deficits, eligible


def _term_subsets(term, completed, courses):
    """该学生在某期、给定已完成集合下全部可行选课子集（含空集）。"""
    available = []
    for cid, capacity in term["offerings"].items():
        if capacity <= 0 or cid in completed:
            continue  # capacity=0 不可用；已取得学分的课不得重复安排
        if set(courses[cid]["prerequisites"]) <= completed:
            available.append(cid)  # 同一期选课互不解锁，先修只看更早的事实
    results = []
    width = len(available)
    for bits in range(1 << width):
        subset = frozenset(available[i] for i in range(width) if bits & (1 << i))
        if sum(courses[cid]["credits"] for cid in subset) <= term["credit_limit"]:
            results.append(subset)
    return results


def _enumerate_student(student_id, initial, already_eligible, terms, courses, program):
    """枚举单个学生的全部完整安排；班额在此阶段不约束（cap=0 已剔除）。"""
    if already_eligible:
        # 已毕业者的任何额外选课都只会更差，空安排在所有维度上不劣。
        empty = (frozenset(),) * len(terms)
        return [{
            "terms": empty, "grad_idx": 0, "credits": 0, "count": 0,
            "enrollments": (), "individual_possible": True,
        }]
    plans = []
    individual_possible = False

    def dive(term_index, completed, chosen):
        nonlocal individual_possible
        if term_index == len(terms):
            grad_idx = None
            state = set(initial)
            for offset, subset in enumerate(chosen, start=1):
                state.update(subset)
                if grad_idx is None:
                    if _graduation(frozenset(state), program, courses)[2]:
                        grad_idx = offset
            if grad_idx is not None:
                individual_possible = True
            enrollments = tuple(sorted(
                (offset, student_id, cid)
                for offset, subset in enumerate(chosen, start=1)
                for cid in subset))
            plans.append({
                "terms": tuple(chosen), "grad_idx": grad_idx,
                "credits": sum(courses[cid]["credits"]
                               for subset in chosen for cid in subset),
                "count": sum(len(subset) for subset in chosen),
                "enrollments": enrollments,
            })
            return
        for subset in _term_subsets(terms[term_index], completed, courses):
            dive(term_index + 1, completed | set(subset), chosen + [subset])

    dive(0, set(initial), [])
    for plan in plans:
        plan["individual_possible"] = individual_possible
    return plans


def _attach_demand(plans, pair_index):
    for plan in plans:
        demand = []
        for offset, subset in enumerate(plan["terms"], start=1):
            for cid in subset:
                demand.append(pair_index[(offset, cid)])
        plan["demand"] = tuple(sorted(demand))


def _solve_group(student_ids, plans_by_student, capacities, term_count):
    """按学生优先序逐人合并，状态为共享名额占用向量；同状态只保留最优前缀。"""
    sentinel = term_count + 1
    zero_usage = (0,) * len(capacities)
    # 状态值：(-毕业人数, 毕业标志键(0=毕业), 毕业期序号, 总学分, 总人次, 选课元组)
    states = {zero_usage: (0, (), (), 0, 0, ())}
    for sid in student_ids:
        next_states = {}
        for usage, key in states.items():
            neg_grads, flags, indices, total_credits, total_count, enrollments = key
            for plan in plans_by_student[sid]:
                updated = list(usage)
                feasible = True
                for slot in plan["demand"]:
                    if updated[slot] + 1 > capacities[slot]:
                        feasible = False
                        break
                    updated[slot] += 1
                if not feasible:
                    continue
                graduates_here = plan["grad_idx"] is not None
                candidate = (
                    neg_grads - (1 if graduates_here else 0),
                    flags + ((0 if graduates_here else 1),),
                    indices + ((plan["grad_idx"] if graduates_here else sentinel),),
                    total_credits + plan["credits"],
                    total_count + plan["count"],
                    tuple(sorted(enrollments + plan["enrollments"])),
                )
                usage_key = tuple(updated)
                incumbent = next_states.get(usage_key)
                if incumbent is None or candidate < incumbent:
                    next_states[usage_key] = candidate
        states = next_states
    return min(states.values()) if states else (0, (), (), 0, 0, ())


def _solve_independent(student_ids, plans_by_student, term_count):
    """班额永远不会卡住任何人时，按学生分别取局部最优再合并（结果与群体 DP 等价）。"""
    sentinel = term_count + 1
    neg_grads = 0
    flags, indices, enrollments = (), (), ()
    total_credits = total_count = 0
    for sid in student_ids:
        def local_key(plan):
            graduated = plan["grad_idx"] is not None
            return (0 if graduated else 1,
                    plan["grad_idx"] if graduated else sentinel,
                    plan["credits"], plan["count"], plan["enrollments"])
        plan = min(plans_by_student[sid], key=local_key)
        graduated = plan["grad_idx"] is not None
        if graduated:
            neg_grads -= 1
        flags += (0 if graduated else 1,)
        indices += (plan["grad_idx"] if graduated else sentinel,)
        total_credits += plan["credits"]
        total_count += plan["count"]
        enrollments = tuple(sorted(enrollments + plan["enrollments"]))
    return neg_grads, flags, indices, total_credits, total_count, enrollments


def plan(data):
    baseline = service.run(data)  # 不可变的规划起点，完整保留 v1/v2 规则
    if "planning" not in data:
        raise ValueError("planning is required")
    cutoff, courses, students, _attempts, program, _transfer = validate(data)
    student_ids, terms = _validate_planning(
        data["planning"], cutoff, courses, students)

    baseline_by_id = {row["student_id"]: row for row in baseline["students"]}
    initial_by_id = {
        sid: {pass_row["course_id"] for pass_row in baseline_by_id[sid]["selected_passes"]}
        for sid in student_ids}

    plans_by_student = {}
    for sid in student_ids:
        already = baseline_by_id[sid]["graduation_eligible"]
        plans_by_student[sid] = _enumerate_student(
            sid, initial_by_id[sid], already, terms, courses, program)

    pair_keys = sorted(
        (offset, cid)
        for offset, term in enumerate(terms, start=1)
        for cid in term["offerings"])
    pair_index = {pair: i for i, pair in enumerate(pair_keys)}
    capacities = tuple(terms[offset - 1]["offerings"][cid]
                       for offset, cid in pair_keys)
    for sid in student_ids:
        _attach_demand(plans_by_student[sid], pair_index)

    if student_ids and capacities and all(cap >= len(student_ids) for cap in capacities):
        best = _solve_independent(student_ids, plans_by_student, len(terms))
    else:
        best = _solve_group(student_ids, plans_by_student, capacities, len(terms))
    neg_grads, flag_keys, completion_indices, planned_credits, enrollment_count, \
        best_enrollments = best

    planned_by_student = {sid: [] for sid in student_ids}
    for offset, sid, cid in best_enrollments:
        planned_by_student[sid].append((offset, cid))

    student_reports = []
    for position, sid in enumerate(student_ids):
        initial = initial_by_id[sid]
        planned = sorted(planned_by_student[sid])
        projected = sorted(initial | {cid for _offset, cid in planned})
        projected_credits = sum(courses[cid]["credits"] for cid in projected)
        missing, deficits, _eligible = _graduation(
            frozenset(projected), program, courses)
        grad_idx = completion_indices[position] if completion_indices else None
        if baseline_by_id[sid]["graduation_eligible"]:
            status = "already_eligible"
        elif grad_idx is not None and grad_idx <= len(terms):
            status = "scheduled"
        elif plans_by_student[sid][0]["individual_possible"]:
            status = "capacity_deferred"
        else:
            status = "unreachable"
        student_reports.append({
            "student_id": sid,
            "status": status,
            "individual_graduation_possible":
                plans_by_student[sid][0]["individual_possible"],
            "graduation_term_index": (
                0 if status == "already_eligible"
                else (grad_idx if status == "scheduled" else None)),
            "graduation_term_id": (
                terms[grad_idx - 1]["id"] if status == "scheduled" else None),
            "planned_courses": [
                {"term_id": terms[offset - 1]["id"], "course_id": cid}
                for offset, cid in planned],
            "projected_completed_courses": projected,
            "projected_credits": projected_credits,
            "missing_required": missing,
            "credit_deficits": deficits,
        })

    term_reports = []
    for offset, term in enumerate(terms, start=1):
        enrollments = sorted(
            (sid, cid) for term_no, sid, cid in best_enrollments if term_no == offset)
        used = {cid: 0 for cid in term["offerings"]}
        for _sid, cid in enrollments:
            used[cid] += 1
        term_reports.append({
            "term_id": term["id"],
            "enrollments": [
                {"student_id": sid, "course_id": cid} for sid, cid in enrollments],
            "seat_usage": [
                {"course_id": cid, "used": used[cid],
                 "capacity": term["offerings"][cid]}
                for cid in sorted(term["offerings"])],
        })

    return {
        "schema_version": 1,
        "as_of": cutoff.isoformat(),
        "baseline_audit": baseline,
        "terms": term_reports,
        "students": student_reports,
        "objective": {
            "graduates": -neg_grads,
            "graduation_flags": [1 - bit for bit in flag_keys],
            "completion_indices": list(completion_indices),
            "planned_credits": planned_credits,
            "enrollments": enrollment_count,
        },
    }
