"""Chronological prerequisite validation and credit selection from the same attempts."""
from fractions import Fraction


def student_result(student_id, rows, courses, program, cutoff):
    completed = set()
    selected = {}
    audits = []
    future = 0
    rows = sorted(rows, key=lambda r: (r["date"], r["id"]))
    # Treat all exams on one date simultaneously: same-day passes cannot unlock prerequisites.
    days = {}
    for row in rows:
        if row["date"] > cutoff.isoformat():
            future += 1
        else:
            days.setdefault(row["date"], []).append(row)
    for when, day_rows in days.items():
        new_passes = set()
        for row in day_rows:
            course_id = row["course_id"]
            course = courses[course_id]
            missing = sorted(set(course["prerequisites"]) - completed)
            status = "blocked_prerequisite" if missing else (
                "passed" if row["mark"] >= course["pass_mark"] else "failed")
            audits.append({"attempt_id": row["id"], "course_id": course_id,
                           "status": status, "missing_prerequisites": missing})
            if status == "passed":
                new_passes.add(course_id)
                before = selected.get(course_id)
                # Highest valid passing mark; earliest date and ID resolve ties.
                if before is None or (-row["mark"], row["date"], row["id"]) < (
                        -before["mark"], before["date"], before["id"]):
                    selected[course_id] = row
        completed.update(new_passes)
    credit_by_category = {c["category"]: 0 for c in courses.values()}
    for course_id in selected:
        c = courses[course_id]
        credit_by_category[c["category"]] += c["credits"]
    deficits = {key: max(0, amount - credit_by_category.get(key, 0))
                for key, amount in sorted(program["minimum_credits"].items())}
    missing_required = sorted(set(program["required_courses"]) - set(selected))
    credits = sum(courses[key]["credits"] for key in selected)
    weighted = sum(courses[key]["credits"] * row["mark"] for key, row in selected.items())
    return {
        "student_id": student_id,
        "selected_passes": [{"course_id": key, "attempt_id": row["id"], "mark": row["mark"],
                             "credits": courses[key]["credits"]} for key, row in sorted(selected.items())],
        "attempt_audit": audits, "ignored_future_attempts": future,
        "credits": credits, "credits_by_category": dict(sorted(credit_by_category.items())),
        "credited_weighted_mark": str(Fraction(weighted, credits)) if credits else None,
        "missing_required": missing_required, "credit_deficits": deficits,
        "graduation_eligible": not missing_required and not any(deficits.values()),
        "eligible_uncredited_courses": sorted(key for key, c in courses.items()
             if key not in selected and set(c["prerequisites"]) <= completed),
    }
