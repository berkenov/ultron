import pathlib
import tempfile
import unittest

from tests.helpers import GATE_CMD, HELLO_DOC, HELLO_ONLY, envelope, make_repo, so_code, write_spec
from ultron import policy
from ultron.check import CheckReport, GateRun, check_all, check_contract, check_scope, load_role_schema, run_gate
from ultron.spec import load_spec


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_role_schema("code")
        self.role = policy.ROLES["code"]

    def test_valid_output_passes(self):
        violations, warnings = check_contract(envelope(so_code()), self.role, self.schema)
        self.assertEqual(violations, [])
        self.assertEqual(warnings, [])

    def test_denied_actions_violate(self):
        violations, _ = check_contract(envelope(None, denied=("RunCommand",)), self.role, self.schema)
        self.assertTrue(any("без прав" in v and "RunCommand" in v for v in violations))

    def test_missing_structured_output_violates(self):
        violations, _ = check_contract(envelope(None), self.role, self.schema)
        self.assertTrue(any("structured_output" in v for v in violations))

    def test_missing_required_fields_named(self):
        so = so_code()
        del so["not_done"]
        del so["commands"]
        violations, _ = check_contract(envelope(so), self.role, self.schema)
        joined = " ".join(violations)
        self.assertIn("not_done", joined)
        self.assertIn("commands", joined)

    def test_status_outside_enum_violates(self):
        violations, _ = check_contract(envelope(so_code(status="success")), self.role, self.schema)
        self.assertTrue(any("status=" in v for v in violations))

    def test_agy_status_not_success_violates(self):
        violations, _ = check_contract(envelope(so_code(), status="TIMEOUT"), self.role, self.schema)
        self.assertTrue(any("TIMEOUT" in v for v in violations))

    def test_caps_are_warnings_not_violations(self):
        so = so_code(summary="x" * (policy.CAPS["summary_chars"] + 1))
        violations, warnings = check_contract(envelope(so), self.role, self.schema)
        self.assertEqual(violations, [])
        self.assertTrue(any("summary" in w for w in warnings))

    def test_done_without_changes_warns_for_code(self):
        _, warnings = check_contract(envelope(so_code(changed=())), self.role, self.schema)
        self.assertTrue(any("changed_files" in w and "patch" in w for w in warnings))


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def spec(self, **fields):
        return load_spec(write_spec(self.tmp, self.repo, **fields))

    def test_readonly_role_any_change_violates(self):
        spec = self.spec(role="explore")
        violations = check_scope(["app/service.py"], spec)
        self.assertEqual(len(violations), 1)
        self.assertIn("app/service.py", violations[0])

    def test_plan_mode_any_change_violates(self):
        spec = self.spec(id="T-2", executor={"mode": "plan"})
        self.assertEqual(len(check_scope(["app/service.py"], spec)), 1)

    def test_write_role_empty_scope_allows_everything(self):
        self.assertEqual(check_scope(["app/service.py", "tests/x.py"], self.spec(id="T-3")), [])

    def test_write_role_outside_write_violates(self):
        spec = self.spec(id="T-4", scope={"write": ["app/service.py"]})
        violations = check_scope(["app/service.py", "tests/test_service.py"], spec)
        self.assertEqual(len(violations), 1)
        self.assertIn("tests/test_service.py", violations[0])
        self.assertIn("вне разрешённого объёма", violations[0])

    def test_directory_prefix_matches(self):
        spec = self.spec(id="T-5", scope={"write": ["app/"], "forbid": ["app/models.py"]})
        self.assertEqual(check_scope(["app/service.py"], spec), [])
        self.assertEqual(len(check_scope(["app/models.py"], spec)), 1)

    def test_forbid_wins_over_write(self):
        spec = self.spec(id="T-6", scope={"write": ["app/"], "forbid": ["app/"]})
        self.assertEqual(len(check_scope(["app/service.py"], spec)), 1)

    def test_no_changes_no_violations(self):
        self.assertEqual(check_scope([], self.spec(role="explore", id="T-7")), [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def spec(self, acceptance, **fields):
        return load_spec(write_spec(self.tmp, self.repo, acceptance=acceptance, **fields))

    def test_red_gate_on_fresh_repo(self):
        runs = run_gate(self.spec([{"cmd": GATE_CMD}]))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].exit_code, 1)
        self.assertFalse(runs[0].ok)
        self.assertTrue(any("FAIL" in line for line in runs[0].tail))

    def test_green_gate_after_fix(self):
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        runs = run_gate(self.spec([{"cmd": GATE_CMD}], id="T-2"))
        self.assertTrue(runs[0].ok)
        self.assertIn("CHECK OK", runs[0].log)

    def test_hidden_check_fails_without_docstring(self):
        (self.repo / "app" / "service.py").write_text(HELLO_ONLY)
        runs = run_gate(self.spec([{"cmd": GATE_CMD}], id="T-3"))
        self.assertFalse(runs[0].ok)
        self.assertTrue(any("docstring" in line for line in runs[0].tail))

    def test_expect_exit_honored_and_stops_at_first_failure(self):
        runs = run_gate(self.spec([{"cmd": "exit 3", "expect_exit": 3}, {"cmd": "exit 1"}, {"cmd": "echo never"}], id="T-4"))
        self.assertEqual([r.ok for r in runs], [True, False])
        self.assertEqual(runs[1].exit_code, 1)

    def test_gate_runs_in_cwd_override(self):
        other = self.tmp / "other"
        import shutil
        shutil.copytree(self.repo, other)
        (other / "app" / "service.py").write_text(HELLO_DOC)
        spec = self.spec([{"cmd": GATE_CMD}], id="T-6")
        self.assertFalse(run_gate(spec)[0].ok)
        self.assertTrue(run_gate(spec, cwd=other)[0].ok)

    def test_tail_length_and_log_truncation(self):
        cmd = "python3 -c \"print('\\n'.join(str(i) for i in range(500)))\"; exit 1"
        runs = run_gate(self.spec([{"cmd": cmd}], id="T-5"))
        self.assertEqual(len(runs[0].tail), policy.GATE_TAIL_LINES)
        self.assertEqual(runs[0].tail[-1], "499")
        self.assertLessEqual(len(runs[0].log.encode()), policy.GATE_LOG_MAX_BYTES + 200)


class CheckAllTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.schema = load_role_schema("code")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_all_green(self):
        spec = load_spec(write_spec(self.tmp, self.repo, acceptance=[{"cmd": GATE_CMD}], scope={"write": ["app/service.py"]}))
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        report = check_all(envelope(so_code()), spec, self.schema, ["app/service.py"])
        self.assertIsInstance(report, CheckReport)
        self.assertTrue(report.ok)
        self.assertEqual(report.violations, ())
        self.assertEqual(len(report.gate), 1)

    def test_gate_runs_even_when_contract_broken(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-2", acceptance=[{"cmd": GATE_CMD}]))
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        report = check_all(envelope(None, denied=("RunCommand",)), spec, self.schema, ["app/service.py"])
        self.assertFalse(report.ok)
        self.assertTrue(report.contract)
        self.assertEqual(len(report.gate), 1)
        self.assertTrue(report.gate[0].ok)

    def test_gate_failure_is_a_violation(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-3", acceptance=[{"cmd": GATE_CMD}]))
        (self.repo / "app" / "service.py").write_text(HELLO_ONLY)
        report = check_all(envelope(so_code()), spec, self.schema, ["app/service.py"])
        self.assertFalse(report.ok)
        self.assertTrue(any("гейт" in v.lower() for v in report.violations))

    def test_scope_violation_reported(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-5", scope={"write": ["app/service.py"]}))
        report = check_all(envelope(so_code()), spec, self.schema, ["app/service.py", "tests/test_service.py"])
        self.assertFalse(report.ok)
        self.assertEqual(len(report.scope), 1)

    def test_readonly_role_without_acceptance_has_no_gate(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-4", role="explore"))
        from tests.helpers import so_explore
        report = check_all(envelope(so_explore()), spec, load_role_schema("explore"), [])
        self.assertTrue(report.ok)
        self.assertEqual(report.gate, ())


if __name__ == "__main__":
    unittest.main()
