from .model import validate
from .credits import student_result
from .transfers import student_transfer_result


def run(data):
    cutoff, courses, students, attempts, program, transfer = validate(data)
    grouped = {key: [] for key in students}
    for attempt in attempts:
        grouped[attempt["student_id"]].append(attempt)
    if transfer is None:
        rows = [student_result(key, grouped[key], courses, program, cutoff)
                for key in sorted(students)]
        schema_version = 1
    else:
        rows = []
        for key in sorted(students):
            options = transfer["by_student"].get(key, [])
            rows.append(student_transfer_result(
                key, grouped[key], options, transfer["credit_limit"],
                courses, program, cutoff))
        schema_version = 2
    return {"schema_version": schema_version, "as_of": cutoff.isoformat(),
            "students": rows,
            "summary": {"students": len(rows),
                        "graduation_eligible": sum(r["graduation_eligible"] for r in rows),
                        "credited_units": sum(r["credits"] for r in rows),
                        "blocked_attempts": sum(
                            a["status"] == "blocked_prerequisite"
                            for r in rows for a in r["attempt_audit"])}}
