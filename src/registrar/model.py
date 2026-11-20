from datetime import date
from .io import integer, text, unique


def parse_day(value):
    if not isinstance(value, str):
        raise ValueError("date must be canonical YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be canonical YYYY-MM-DD") from None
    if result.isoformat() != value:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return result


def _ref_array(value, label, known):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a nonempty array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must contain strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")
    for item in value:
        if item not in known:
            raise ValueError(f"unknown {label} reference: {item}")
    return list(value)


def _require_id_rows(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be an array")
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            raise ValueError(f"{label} rows require an id")
        text(row["id"], f"{label}.id")
    return unique(rows, label)


def _validate_transfer(node, students, courses):
    if not isinstance(node, dict):
        raise ValueError("transfer must be an object")
    for field in ("evidence", "options", "credit_limit"):
        if field not in node:
            raise ValueError(f"transfer.{field} is required")
    credit_limit = integer(node["credit_limit"], "transfer.credit_limit", 0)
    evidence_rows = _require_id_rows(node["evidence"], "evidence")
    if len(evidence_rows) > 20000:
        raise ValueError("at most 20000 evidence records")
    evidence = {}
    for eid, ev in evidence_rows.items():
        if "student_id" not in ev or "date" not in ev:
            raise ValueError("evidence requires student_id and date")
        sid = text(ev["student_id"], "evidence.student_id")
        if sid not in students:
            raise ValueError("unknown evidence student")
        evidence[eid] = {"id": eid, "student_id": sid, "date": parse_day(ev["date"])}
    raw_options = node["options"]
    if not isinstance(raw_options, list):
        raise ValueError("options must be an array")
    for row in raw_options:
        if not isinstance(row, dict) or "id" not in row:
            raise ValueError("option rows require an id")
        text(row["id"], "option.id")
    options = {}
    by_student = {}
    for row in raw_options:
        if not isinstance(row, dict):
            raise ValueError("option rows must be objects")
        for field in ("id", "student_id", "evidence_ids", "course_ids", "effective_date"):
            if field not in row:
                raise ValueError(f"option.{field} is required")
        oid = text(row["id"], "option.id")
        if oid in options:
            raise ValueError(f"duplicate option id: {oid}")
        sid = text(row["student_id"], "option.student_id")
        if sid not in students:
            raise ValueError("unknown option student")
        evidence_ids = _ref_array(row["evidence_ids"], "option.evidence_ids", evidence)
        for ref in evidence_ids:
            if evidence[ref]["student_id"] != sid:
                raise ValueError("evidence not owned by option student")
        course_ids = _ref_array(row["course_ids"], "option.course_ids", courses)
        effective_date = parse_day(row["effective_date"])
        for ref in evidence_ids:
            if effective_date < evidence[ref]["date"]:
                raise ValueError("effective_date precedes evidence date")
        record = {"id": oid, "student_id": sid, "evidence_ids": evidence_ids,
                  "course_ids": course_ids, "effective_date": effective_date}
        options[oid] = record
        by_student.setdefault(sid, []).append(record)
    if len(options) > 2000:
        raise ValueError("at most 2000 options")
    for sid, items in by_student.items():
        if len(items) > 12:
            raise ValueError(f"student {sid} has more than 12 options")
    return {"credit_limit": credit_limit, "evidence": evidence,
            "options": options, "by_student": by_student}


def validate(data):
    cutoff = parse_day(data["as_of"])
    courses = unique(data["courses"], "course")
    students = unique(data["students"], "student")
    attempts = unique(data["attempts"], "attempt")
    if not 1 <= len(courses) <= 200 or len(students) > 2000 or len(attempts) > 20000:
        raise ValueError("limits: 1..200 courses, 2000 students, 20000 attempts")
    for course in courses.values():
        integer(course["credits"], "credits", 1)
        integer(course["pass_mark"], "pass_mark")
        if course["pass_mark"] > 100:
            raise ValueError("pass_mark exceeds 100")
        text(course["category"], "category")
        prerequisites = course["prerequisites"]
        if not isinstance(prerequisites, list) or len(set(prerequisites)) != len(prerequisites):
            raise ValueError("prerequisites must be a unique array")
        if set(prerequisites) - set(courses):
            raise ValueError("unknown prerequisite")
    visited, active = set(), set()

    def visit(key):
        if key in active:
            raise ValueError("prerequisite cycle")
        if key in visited:
            return
        active.add(key)
        for parent in courses[key]["prerequisites"]:
            visit(parent)
        active.remove(key)
        visited.add(key)

    for key in courses:
        visit(key)
    program = data["program"]
    required = program["required_courses"]
    if not isinstance(required, list) or len(set(required)) != len(required) or set(required) - set(courses):
        raise ValueError("invalid required course IDs")
    categories = {c["category"] for c in courses.values()}
    minimums = program["minimum_credits"]
    if not isinstance(minimums, dict) or set(minimums) - categories:
        raise ValueError("unknown credit category")
    for key, value in minimums.items():
        integer(value, f"minimum_credits.{key}")
    seen = set()
    for attempt in attempts.values():
        if attempt["student_id"] not in students or attempt["course_id"] not in courses:
            raise ValueError("unknown attempt student/course")
        when = parse_day(attempt["date"])
        mark = integer(attempt["mark"], "mark")
        if mark > 100:
            raise ValueError("mark exceeds 100")
        key = (attempt["student_id"], attempt["course_id"], when)
        if key in seen:
            raise ValueError("duplicate student/course/day attempt")
        seen.add(key)
    transfer = _validate_transfer(data["transfer"], students, courses) if "transfer" in data else None
    return cutoff, courses, students, list(attempts.values()), program, transfer
