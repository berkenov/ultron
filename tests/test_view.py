import unittest

from tests.helpers import result_dict, so_code
from ultron import policy
from ultron.view import compact, evidence_text


class CompactTests(unittest.TestCase):
    def test_header_and_core_fields(self):
        text = compact(result_dict())
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("=== T-1 | code | accepted | attempts 1 | tier gemini-3.1-pro-high"))
        self.assertIn("summary: Приветствие заменено.", text)
        self.assertIn("app/service.py", text)
        self.assertIn("e1 app/service.py:5", text)
        self.assertIn("next_action: ничего", text)
        self.assertTrue(lines[-1].startswith("state: "))

    def test_role_is_taken_from_refs_or_role_field(self):
        text = compact(result_dict(role="explore", role_fields={"map": [{"path": "a.py", "purpose": "п", "depends_on": []}], "facts": []}))
        self.assertIn("explore", text.splitlines()[0])
        self.assertIn("map (1)", text)

    def test_excerpts_never_shown(self):
        text = compact(result_dict(evidence=[{"id": "e1", "where": "a.py:1", "excerpt": "SECRET EXCERPT"}]))
        self.assertNotIn("SECRET EXCERPT", text)

    def test_claimed_shown_only_on_mismatch(self):
        self.assertNotIn("claimed", compact(result_dict()))
        text = compact(result_dict(claimed_files=["app/service.py", "app/models.py"]))
        self.assertIn("claimed", text)
        self.assertIn("app/models.py", text)

    def test_violations_shown_when_not_accepted(self):
        res = result_dict(status="rejected", checks={"contract": "ok", "scope": "violated", "gate": None, "violations": ["вне разрешённого объёма: tests/x.py"]})
        text = compact(res)
        self.assertIn("rejected", text.splitlines()[0])
        self.assertIn("violations", text)
        self.assertIn("tests/x.py", text)

    def test_checks_line_always_filled(self):
        self.assertIn("checks: contract ok · scope ok · gate [0]", compact(result_dict()))
        rejected = result_dict(status="rejected", checks={"contract": "ok", "scope": "violated", "gate": None, "violations": ["x"]})
        self.assertIn("checks: contract ok · scope violated · gate —", compact(rejected))

    def test_restored_and_failure_lines(self):
        res = result_dict(status="rejected", checks={"contract": "ok", "scope": "restored", "gate": None, "violations": ["x"], "restored": ["tools/check.py"]}, failure={"domain": "execution", "kind": "gate"})
        text = compact(res)
        self.assertIn("restored: tools/check.py", text)
        self.assertIn("failure: execution/gate", text)
        self.assertNotIn("failure:", compact(result_dict()))

    def test_seconds_rounded_in_header(self):
        text = compact(result_dict(usage={"executor_in": 100, "cache_read": 20, "executor_out": 10, "thinking": 5, "seconds": 32.835173}))
        self.assertIn("| 33 s |", text.splitlines()[0])
        self.assertNotIn("32.8", text)

    def test_open_questions_listed(self):
        text = compact(result_dict(status="blocked", open_questions=["Какую локаль?", "Нужен ли кэш?"]))
        self.assertIn("open_questions (2)", text)
        self.assertIn("Какую локаль?", text)

    def test_length_cap_with_pointer(self):
        big = result_dict(role_fields={"map": [{"path": f"app/f{i}.py", "purpose": "файл", "depends_on": []} for i in range(120)], "facts": []}, role="explore")
        text = compact(big)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), policy.COMPACT_MAX_LINES)
        self.assertIn("ultron show T-1 --raw", lines[-1])

    def test_no_line_starts_with_brace(self):
        text = compact(result_dict(role_fields={"changed_files": [], "commands": [{"cmd": "x", "exit_code": 0, "summary": "{ok}"}], "not_done": [], "patch": ""}))
        self.assertFalse(any(line.lstrip().startswith("{") for line in text.splitlines()))

    def test_verify_sections(self):
        rf = {"gate": {"command": "pytest", "exit_code": 1, "passed": 3, "failed": 1, "skipped": 0},
              "failures": [{"test": "test_a", "where": "tests/a.py:9", "reason": "ожидалось 1", "suspected_cause": "off by one"}], "warnings": ["W1"]}
        text = compact(result_dict(role="verify", role_fields=rf))
        self.assertIn("gate:", text)
        self.assertIn("failed 1", text)
        self.assertIn("test_a", text)
        self.assertIn("off by one", text)

    def test_review_sections(self):
        rf = {"verdicts": [{"claim": "A", "verdict": "false", "where": "x.py:1", "note": "нет"}, {"claim": "B", "verdict": "verified", "where": "y.py:2", "note": ""}],
              "risks": [{"risk": "R", "where": "z.py:3", "mitigation": "M"}], "recommendation": {"choice": "делать", "reasoning": "потому"}}
        text = compact(result_dict(role="review", role_fields=rf))
        self.assertIn("verdicts (2)", text)
        self.assertIn("[false] A", text)
        self.assertNotIn("[verified] B", text)
        self.assertIn("recommendation: делать", text)


class EvidenceTests(unittest.TestCase):
    def test_excerpt_from_last_attempt(self):
        attempts = [
            {"structured_output": so_code(evidence=[{"id": "e1", "where": "a.py:1", "excerpt": "old"}])},
            {"status": "SUCCESS", "structured_output": so_code(evidence=[{"id": "e1", "where": "a.py:2", "excerpt": "new"}])},
        ]
        self.assertEqual(evidence_text(attempts, "e1"), "e1 @ a.py:2\nnew")

    def test_unknown_id_is_none(self):
        self.assertIsNone(evidence_text([{"structured_output": so_code()}], "e9"))
        self.assertIsNone(evidence_text([], "e1"))


if __name__ == "__main__":
    unittest.main()
