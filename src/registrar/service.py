from .model import validate
from .credits import student_result


def run(data):
    cutoff, courses, students, attempts, program = validate(data)
    grouped = {key: [] for key in students}
    for attempt in attempts:
        grouped[attempt["student_id"]].append(attempt)
    rows = [student_result(key, grouped[key], courses, program, cutoff) for key in sorted(students)]
    return {"schema_version": 1, "as_of": cutoff.isoformat(), "students": rows,
            "summary": {"students": len(rows), "graduation_eligible": sum(r["graduation_eligible"] for r in rows),
                        "credited_units": sum(r["credits"] for r in rows),
                        "blocked_attempts": sum(a["status"] == "blocked_prerequisite" for r in rows for a in r["attempt_audit"])}}
