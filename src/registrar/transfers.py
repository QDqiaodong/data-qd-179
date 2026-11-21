"""Schema v2: holistic transfer-credit recognition plans.

A transfer option is atomic: when selected, every referenced evidence item is
consumed and every target course is recognized as a pass at its effective
date.  Feasible option subsets are enumerated per student, each subset drives
a full re-audit of every exam, and the subset with the best whole-program
outcome wins.  No option is ever applied independently and deduplicated later.

The enumeration and the full audit are exposed separately so that other
commands (the primary/minor joint review) can evaluate every feasible plan
from scratch instead of reusing the plan selected by ``credit-audit``.
"""
from fractions import Fraction


def _audit(student_rows, chosen, courses, program, cutoff):
    """Re-run the day-by-day rules with transfer passes injected on time."""
    days = {}
    future = 0
    for row in student_rows:
        if row["date"] > cutoff.isoformat():
            future += 1
        else:
            days.setdefault(row["date"], []).append(row)
    transfer_days = {}
    for option in chosen:
        transfer_days.setdefault(option["effective_date"].isoformat(), []).append(option)
    completed = set()          # courses with any usable pass evidence
    exam_passes = {}           # course -> best legal exam pass row
    audits = []
    all_days = sorted(set(days) | set(transfer_days))
    for when in all_days:
        new_passes = set()
        # Exams are judged only against evidence from strictly earlier days;
        # transfers effective today unlock nothing until tomorrow.
        for row in days.get(when, []):
            course_id = row["course_id"]
            course = courses[course_id]
            missing = sorted(set(course["prerequisites"]) - completed)
            status = "blocked_prerequisite" if missing else (
                "passed" if row["mark"] >= course["pass_mark"] else "failed")
            audits.append({"attempt_id": row["id"], "course_id": course_id,
                           "status": status, "missing_prerequisites": missing})
            if status == "passed":
                new_passes.add(course_id)
                before = exam_passes.get(course_id)
                # Highest valid passing mark; earliest date and ID resolve ties.
                if before is None or (-row["mark"], row["date"], row["id"]) < (
                        -before["mark"], before["date"], before["id"]):
                    exam_passes[course_id] = row
        for option in transfer_days.get(when, []):
            new_passes.update(option["course_ids"])
        # Same-day events never unlock each other: state changes apply after.
        completed.update(new_passes)
    return completed, exam_passes, audits, future


def _evaluate(chosen, student_rows, courses, program, cutoff):
    completed, exam_passes, audits, future = _audit(
        student_rows, chosen, courses, program, cutoff)
    transfer_courses = {}
    for option in chosen:
        for course_id in option["course_ids"]:
            transfer_courses.setdefault(course_id, option["id"])
    selected_passes = []
    credit_by_category = {c["category"]: 0 for c in courses.values()}
    weighted = 0
    weighted_credits = 0
    for course_id in sorted(set(exam_passes) | set(transfer_courses)):
        course = courses[course_id]
        credit_by_category[course["category"]] += course["credits"]
        if course_id in exam_passes:
            row = exam_passes[course_id]
            selected_passes.append({"course_id": course_id, "attempt_id": row["id"],
                                    "mark": row["mark"], "credits": course["credits"],
                                    "source": "exam", "option_id": None})
            weighted += course["credits"] * row["mark"]
            weighted_credits += course["credits"]
        else:
            selected_passes.append({"course_id": course_id, "attempt_id": None,
                                    "mark": None, "credits": course["credits"],
                                    "source": "transfer",
                                    "option_id": transfer_courses[course_id]})
    selected_courses = {item["course_id"] for item in selected_passes}
    deficits = {key: max(0, amount - credit_by_category.get(key, 0))
                for key, amount in sorted(program["minimum_credits"].items())}
    missing_required = sorted(set(program["required_courses"]) - selected_courses)
    credits = sum(courses[key]["credits"] for key in selected_courses)
    capped_category_credits = sum(
        min(credit_by_category.get(key, 0), amount)
        for key, amount in program["minimum_credits"].items())
    required_covered = len(set(program["required_courses"]) & selected_courses)
    graduation = not missing_required and not any(deficits.values())
    return {
        "completed": completed, "selected_passes": selected_passes,
        "attempt_audit": audits, "ignored_future_attempts": future,
        "credits": credits, "credits_by_category": dict(sorted(credit_by_category.items())),
        "credited_weighted_mark": str(Fraction(weighted, weighted_credits)) if weighted_credits else None,
        "missing_required": missing_required, "credit_deficits": deficits,
        "graduation_eligible": graduation,
        "eligible_uncredited_courses": sorted(
            key for key, c in courses.items()
            if key not in selected_courses and set(c["prerequisites"]) <= completed),
        "objective": (graduation, required_covered, capped_category_credits, credits),
    }


def feasible_plans(available, credit_limit, courses):
    """Enumerate every feasible subset of cutoff-effective transfer options.

    The original package rules apply unchanged: shared evidence items and
    repeated target courses make two options mutually exclusive, and the sum
    of each option's full target-course cost must stay within the per-student
    credit limit.  The empty plan is always included.  Plans are yielded as
    tuples of option records in the order given; each tuple is paired with its
    charged quota cost.  Enumeration is structural: it never expands arrays by
    credit values.
    """
    cost = {opt["id"]: sum(courses[cid]["credits"] for cid in opt["course_ids"])
            for opt in available}
    plans = []

    def search(index, chosen, used_evidence, used_courses, spent):
        if index == len(available):
            plans.append((tuple(chosen), spent))
            return
        option = available[index]
        search(index + 1, chosen, used_evidence, used_courses, spent)
        charge = cost[option["id"]]
        if (spent + charge <= credit_limit
                and not (used_evidence & set(option["evidence_ids"]))
                and not (used_courses & set(option["course_ids"]))):
            search(index + 1, chosen + [option],
                   used_evidence | set(option["evidence_ids"]),
                   used_courses | set(option["course_ids"]),
                   spent + charge)

    search(0, [], set(), set(), 0)
    return plans


def build_transfer_student_row(student_id, rows, chosen, charged,
                               courses, program, cutoff):
    """The original v2 student report row for one chosen feasible plan."""
    result = _evaluate(chosen, rows, courses, program, cutoff)
    return {
        "student_id": student_id,
        "selected_passes": result["selected_passes"],
        "attempt_audit": result["attempt_audit"],
        "ignored_future_attempts": result["ignored_future_attempts"],
        "credits": result["credits"],
        "credits_by_category": result["credits_by_category"],
        "credited_weighted_mark": result["credited_weighted_mark"],
        "missing_required": result["missing_required"],
        "credit_deficits": result["credit_deficits"],
        "graduation_eligible": result["graduation_eligible"],
        "eligible_uncredited_courses": result["eligible_uncredited_courses"],
        "recognition_plan": {
            "option_ids": sorted(opt["id"] for opt in chosen),
            "charged_credits": charged,
            "objective": {
                "graduation_eligible": result["objective"][0],
                "required_covered": result["objective"][1],
                "capped_category_credits": result["objective"][2],
                "credits": result["objective"][3],
            },
        },
    }


def student_transfer_result(student_id, rows, options, credit_limit, courses, program, cutoff):
    rows = sorted(rows, key=lambda r: (r["date"], r["id"]))
    available = [opt for opt in options if opt["effective_date"] <= cutoff]
    best = None  # (comparison key, chosen, charged)

    def key_for(chosen, charged, result):
        graduation, required_covered, capped_category_credits, credits = result["objective"]
        ids = sorted(opt["id"] for opt in chosen)
        return ((not graduation, -required_covered, -capped_category_credits, -credits),
                charged, len(chosen), ids)

    for chosen, charged in feasible_plans(available, credit_limit, courses):
        result = _evaluate(chosen, rows, courses, program, cutoff)
        key = key_for(chosen, charged, result)
        if best is None or key < best[0]:
            best = (key, chosen, charged, result)

    _key, chosen, charged, result = best
    return build_transfer_student_row(
        student_id, rows, chosen, charged, courses, program, cutoff)
