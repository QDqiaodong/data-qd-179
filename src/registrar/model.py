from datetime import date
from .io import integer, text, unique


def parse_day(value):
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return result


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
    return cutoff, courses, students, list(attempts.values()), program
