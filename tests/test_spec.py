import json
import pathlib
import tempfile
import unittest

from tests.helpers import make_repo, write_spec
from ultron import policy
from ultron.spec import Spec, SpecError, load_spec, parse_spec


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_minimal_spec_gets_policy_defaults(self):
        spec = load_spec(write_spec(self.tmp, self.repo))
        self.assertIsInstance(spec, Spec)
        self.assertEqual(spec.role, "code")
        self.assertEqual(spec.dir, self.repo)
        self.assertTrue(spec.brief.is_absolute() and spec.brief.exists())
        self.assertEqual(spec.scope.write, ())
        self.assertEqual(spec.scope.forbid, ())
        self.assertEqual(spec.inputs, ())
        self.assertEqual(spec.acceptance, ())
        self.assertEqual(spec.budget.tiers, policy.ROLES["code"].tiers)
        self.assertEqual(spec.budget.attempts_per_tier, policy.DEFAULT_BUDGET.attempts_per_tier)
        self.assertEqual(spec.budget.executor_tokens, policy.DEFAULT_BUDGET.executor_tokens)
        self.assertEqual(spec.budget.minutes, policy.DEFAULT_BUDGET.minutes)
        self.assertIsNone(spec.executor.model)
        self.assertTrue(spec.writes)
        self.assertIs(spec.role_policy, policy.ROLES["code"])

    def test_readonly_role_does_not_write(self):
        spec = load_spec(write_spec(self.tmp, self.repo, role="explore", id="T-2"))
        self.assertFalse(spec.writes)

    def test_plan_mode_does_not_write(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-3", executor={"mode": "plan"}))
        self.assertFalse(spec.writes)
        self.assertEqual(spec.executor.mode, "plan")

    def test_unknown_role_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec(write_spec(self.tmp, self.repo, role="wizard", id="T-4"))
        self.assertIn("role", str(ctx.exception))

    def test_missing_required_field_named(self):
        with self.assertRaises(SpecError) as ctx:
            parse_spec({"id": "T-5", "role": "code", "dir": str(self.repo)}, self.tmp)
        self.assertIn("brief", str(ctx.exception))

    def test_unknown_key_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec(write_spec(self.tmp, self.repo, id="T-6", bonus=1))
        self.assertIn("bonus", str(ctx.exception))

    def test_unknown_nested_key_rejected(self):
        with self.assertRaises(SpecError) as ctx:
            load_spec(write_spec(self.tmp, self.repo, id="T-7", scope={"write": [], "allow": ["x"]}))
        self.assertIn("allow", str(ctx.exception))

    def test_bad_id_rejected(self):
        with self.assertRaises(SpecError):
            load_spec(write_spec(self.tmp, self.repo, id="T 8"))

    def test_dir_must_be_absolute_and_exist(self):
        with self.assertRaises(SpecError):
            parse_spec({"id": "T-9", "role": "code", "dir": "relative/dir", "brief": "b.md"}, self.tmp)
        with self.assertRaises(SpecError):
            parse_spec({"id": "T-9", "role": "code", "dir": str(self.tmp / "missing"), "brief": "b.md"}, self.tmp)

    def test_brief_must_exist(self):
        with self.assertRaises(SpecError) as ctx:
            parse_spec({"id": "T-10", "role": "code", "dir": str(self.repo), "brief": "nope.md"}, self.tmp)
        self.assertIn("brief", str(ctx.exception))

    def test_executor_model_replaces_first_tier(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-11", executor={"model": "gemini-3.8-flash-medium"}))
        self.assertEqual(spec.budget.tiers[0], "gemini-3.8-flash-medium")
        self.assertEqual(spec.budget.tiers[1:], policy.ROLES["code"].tiers[1:])

    def test_budget_overrides(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-12", budget={"attempts_per_tier": 1, "tiers": ["m1", "m2"], "executor_tokens": 10, "minutes": 3}))
        self.assertEqual(spec.budget.attempts_per_tier, 1)
        self.assertEqual(spec.budget.tiers, ("m1", "m2"))
        self.assertEqual(spec.budget.executor_tokens, 10)
        self.assertEqual(spec.budget.minutes, 3)

    def test_scope_inputs_acceptance_parsed(self):
        spec = load_spec(write_spec(
            self.tmp, self.repo, id="T-13",
            scope={"write": ["app/"], "forbid": ["app/models.py"]},
            inputs=[{"task": "T-1"}, {"task": "T-2", "fields": ["map", "facts"]}],
            acceptance=[{"cmd": "true"}, {"cmd": "false", "expect_exit": 1}],
        ))
        self.assertEqual(spec.scope.write, ("app/",))
        self.assertEqual(spec.scope.forbid, ("app/models.py",))
        self.assertEqual(spec.inputs[0].fields, ("summary",))
        self.assertEqual(spec.inputs[1].fields, ("map", "facts"))
        self.assertEqual(spec.acceptance[0].expect_exit, 0)
        self.assertEqual(spec.acceptance[1].expect_exit, 1)

    def test_workspace_defaults_and_parse(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-15"))
        self.assertEqual(spec.workspace.mode, "inplace")
        self.assertEqual(spec.workspace.seed, ())
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-16", workspace={"mode": "worktree", "seed": ["true", "echo ok"]}))
        self.assertEqual(spec.workspace.mode, "worktree")
        self.assertEqual(spec.workspace.seed, ("true", "echo ok"))
        with self.assertRaises(SpecError) as ctx:
            load_spec(write_spec(self.tmp, self.repo, id="T-17", workspace={"mode": "vm"}))
        self.assertIn("mode", str(ctx.exception))

    def test_max_diff_lines_parsed(self):
        spec = load_spec(write_spec(self.tmp, self.repo, id="T-18", scope={"max_diff_lines": 3}))
        self.assertEqual(spec.scope.max_diff_lines, 3)

    def test_max_diff_lines_validation(self):
        with self.assertRaises(SpecError) as ctx1:
            load_spec(write_spec(self.tmp, self.repo, id="T-19", scope={"max_diff_lines": 0}))
        self.assertIn("max_diff_lines", str(ctx1.exception))
        
        with self.assertRaises(SpecError) as ctx2:
            load_spec(write_spec(self.tmp, self.repo, id="T-20", scope={"max_diff_lines": "3"}))
        self.assertIn("max_diff_lines", str(ctx2.exception))

    def test_load_spec_reads_json_file(self):
        p = write_spec(self.tmp, self.repo, id="T-14", goal="цель")
        spec = load_spec(p)
        self.assertEqual(spec.goal, "цель")
        self.assertEqual(json.loads(p.read_text())["id"], "T-14")


if __name__ == "__main__":
    unittest.main()
