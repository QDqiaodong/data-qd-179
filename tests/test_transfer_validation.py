import copy
import unittest
from registrar.service import run


def base_input():
    return {
        "as_of": "2026-06-01",
        "courses": [
            {"id": "A", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "B", "credits": 3, "pass_mark": 60, "category": "STEM",
             "prerequisites": []},
            {"id": "C", "credits": 4, "pass_mark": 60, "category": "STEM",
             "prerequisites": ["A"]},
            {"id": "D", "credits": 2, "pass_mark": 60, "category": "arts",
             "prerequisites": []},
        ],
        "students": [{"id": "s1"}, {"id": "s2"}],
        "program": {"required_courses": ["A", "C"],
                    "minimum_credits": {"STEM": 7, "arts": 2}},
        "attempts": [
            {"id": "d1", "student_id": "s1", "course_id": "D",
             "date": "2026-02-01", "mark": 70},
            {"id": "c1", "student_id": "s1", "course_id": "C",
             "date": "2026-03-01", "mark": 80},
        ],
    }


def transfer_node():
    return {
        "credit_limit": 6,
        "evidence": [
            {"id": "e1", "student_id": "s1", "date": "2026-01-01"},
            {"id": "e2", "student_id": "s2", "date": "2026-01-02"},
        ],
        "options": [
            {"id": "aa", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B"], "effective_date": "2026-01-31"},
            {"id": "zz", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["A"], "effective_date": "2026-01-31"},
        ],
    }


def with_transfer(**overrides):
    data = base_input()
    node = transfer_node()
    node.update(overrides)
    data["transfer"] = node
    return data


class TransferValidationTests(unittest.TestCase):
    def assert_rejected(self, data, fragment):
        with self.assertRaisesRegex(ValueError, fragment):
            run(data)

    def test_missing_transfer_fields_rejected(self):
        for field in ("evidence", "options", "credit_limit"):
            data = with_transfer()
            del data["transfer"][field]
            self.assert_rejected(data, "transfer")

    def test_missing_evidence_and_option_fields_rejected(self):
        data = with_transfer()
        del data["transfer"]["evidence"][0]["date"]
        self.assert_rejected(data, "evidence")
        data = with_transfer()
        del data["transfer"]["options"][0]["course_ids"]
        self.assert_rejected(data, "option")

    def test_credit_limit_rejects_boolean_and_negative(self):
        data = with_transfer(credit_limit=True)
        self.assert_rejected(data, "credit_limit")
        data = with_transfer(credit_limit=-1)
        self.assert_rejected(data, "credit_limit")

    def test_duplicate_ids_rejected(self):
        data = with_transfer()
        data["transfer"]["evidence"].append(
            {"id": "e1", "student_id": "s1", "date": "2026-02-01"})
        self.assert_rejected(data, "duplicate evidence")
        data = with_transfer()
        dup = copy.deepcopy(data["transfer"]["options"][0])
        dup["evidence_ids"] = ["e1"]
        data["transfer"]["options"].append(dup)
        self.assert_rejected(data, "duplicate option")

    def test_unknown_student_evidence_and_option_rejected(self):
        data = with_transfer()
        data["transfer"]["evidence"][0]["student_id"] = "ghost"
        self.assert_rejected(data, "evidence student")
        data = with_transfer()
        data["transfer"]["options"][0]["student_id"] = "ghost"
        self.assert_rejected(data, "option student")

    def test_unknown_and_malformed_references_rejected(self):
        data = with_transfer()
        data["transfer"]["options"][0]["evidence_ids"] = ["nope"]
        self.assert_rejected(data, "evidence_ids")
        data = with_transfer()
        data["transfer"]["options"][0]["course_ids"] = ["NOPE"]
        self.assert_rejected(data, "course_ids")
        data = with_transfer()
        data["transfer"]["options"][0]["evidence_ids"] = []
        self.assert_rejected(data, "evidence_ids")
        data = with_transfer()
        data["transfer"]["options"][0]["course_ids"] = ["A", "A"]
        self.assert_rejected(data, "course_ids")
        data = with_transfer()
        data["transfer"]["options"][0]["evidence_ids"] = ["e1", "e1"]
        self.assert_rejected(data, "evidence_ids")
        data = with_transfer()
        data["transfer"]["options"][0]["evidence_ids"] = "e1"
        self.assert_rejected(data, "evidence_ids")

    def test_evidence_ownership_enforced(self):
        data = with_transfer()
        data["transfer"]["options"][0]["evidence_ids"] = ["e2"]
        self.assert_rejected(data, "not owned")

    def test_effective_date_rules(self):
        data = with_transfer()
        data["transfer"]["options"][0]["effective_date"] = "2025-12-31"
        self.assert_rejected(data, "effective_date")
        data = with_transfer()
        data["transfer"]["options"][0]["effective_date"] = "2026-1-31"
        self.assert_rejected(data, "canonical")
        data = with_transfer()
        data["transfer"]["evidence"][0]["date"] = "not-a-date"
        self.assert_rejected(data, "YYYY-MM-DD")

    def test_future_option_with_bad_structure_still_rejected(self):
        # A future option is never selectable, but structural validation must
        # not be skipped just because it is unavailable as of the cutoff.
        data = with_transfer()
        data["transfer"]["options"].append(
            {"id": "future-bad", "student_id": "s1", "evidence_ids": ["ghost"],
             "course_ids": ["A"], "effective_date": "2030-01-01"})
        self.assert_rejected(data, "evidence_ids")
        data = with_transfer()
        data["transfer"]["evidence"].append(
            {"id": "e9", "student_id": "ghost", "date": "2030-01-01"})
        self.assert_rejected(data, "evidence student")

    def test_future_records_are_valid_but_unselectable(self):
        data = with_transfer()
        data["transfer"]["options"].append(
            {"id": "future-ok", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["D"], "effective_date": "2030-01-01"})
        result = run(data)
        self.assertEqual(result["schema_version"], 2)
        plan = result["students"][0]["recognition_plan"]
        self.assertNotIn("future-ok", plan["option_ids"])

    def test_unreferenced_evidence_is_allowed(self):
        data = with_transfer()
        data["transfer"]["evidence"].append(
            {"id": "lonely", "student_id": "s1", "date": "2026-01-01"})
        run(data)  # no exception

    def test_per_student_option_limit(self):
        data = with_transfer()
        data["transfer"]["options"] = [
            {"id": f"o{i:02d}", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}
            for i in range(13)]
        self.assert_rejected(data, "12 options")

    def test_global_array_limits(self):
        data = with_transfer()
        data["transfer"]["options"] = [
            {"id": f"o{i:04d}", "student_id": "s1", "evidence_ids": ["e1"],
             "course_ids": ["B"], "effective_date": "2026-01-31"}
            for i in range(2001)]
        self.assert_rejected(data, "2000 options")
        data = with_transfer()
        data["transfer"]["evidence"] = [
            {"id": f"ev{i:05d}", "student_id": "s1", "date": "2026-01-01"}
            for i in range(20001)]
        self.assert_rejected(data, "20000 evidence")

    def test_rows_without_id_or_wrong_container_rejected(self):
        data = with_transfer()
        del data["transfer"]["evidence"][0]["id"]
        self.assert_rejected(data, "evidence")
        data = with_transfer()
        del data["transfer"]["options"][0]["id"]
        self.assert_rejected(data, "option")
        data = with_transfer()
        data["transfer"]["evidence"] = {"id": "e1"}
        self.assert_rejected(data, "evidence")
        data = with_transfer()
        data["transfer"]["options"][0]["id"] = 42
        self.assert_rejected(data, "option.id")
        data = with_transfer()
        data["transfer"]["credit_limit"] = 3.0
        self.assert_rejected(data, "credit_limit")


if __name__ == "__main__":
    unittest.main()
