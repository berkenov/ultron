import pathlib
import tempfile
import unittest

from tests.helpers import GATE_CMD, HELLO_DOC, HELLO_ONLY, FakeExecutor, envelope, git, make_repo, so_code, write_spec
from ultron.run import run_task
from ultron.snapshot import patch_between, take_snapshot
from ultron.spec import load_spec
from ultron.state import TaskState, project_slug
from ultron.workspace import WorkspaceError, create_worktree, integrate, remove_worktree, seed, worktree_path


class WorktreePrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_worktree_path_under_state(self):
        p = worktree_path(self.root, self.repo, "T-1")
        self.assertEqual(p, self.root / project_slug(self.repo) / "worktrees" / "T-1")

    def test_create_from_baseline_includes_uncommitted_state_and_remove(self):
        (self.repo / "app" / "service.py").write_text(HELLO_ONLY)
        (self.repo / "notes.md").write_text("draft")
        base = take_snapshot(self.repo, "T-1", "baseline")
        path = worktree_path(self.root, self.repo, "T-1")
        created = create_worktree(self.repo, path, base.commit)
        self.assertEqual(created, path)
        self.assertEqual((path / "app" / "service.py").read_text(), HELLO_ONLY)
        self.assertEqual((path / "notes.md").read_text(), "draft")
        self.assertEqual(git(path, "rev-parse", "HEAD").strip(), base.commit)
        self.assertIn(str(path), git(self.repo, "worktree", "list"))
        create_worktree(self.repo, path, base.commit)
        self.assertTrue((path / "notes.md").exists())
        remove_worktree(self.repo, path)
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(self.repo, "worktree", "list"))
        remove_worktree(self.repo, path)

    def test_create_and_remove_are_silent(self):
        import subprocess
        import sys as _sys
        base = take_snapshot(self.repo, "T-1", "baseline")
        path = worktree_path(self.root, self.repo, "T-1")
        root = str(pathlib.Path(__file__).resolve().parents[1])
        code = ("import pathlib, sys; sys.path.insert(0, %r); from ultron.workspace import create_worktree, remove_worktree; "
                "create_worktree(pathlib.Path(%r), pathlib.Path(%r), %r); remove_worktree(pathlib.Path(%r), pathlib.Path(%r))"
                % (root, str(self.repo), str(path), base.commit, str(self.repo), str(path)))
        proc = subprocess.run([_sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_seed_runs_commands_and_fails_loudly(self):
        base = take_snapshot(self.repo, "T-1", "baseline")
        path = create_worktree(self.repo, worktree_path(self.root, self.repo, "T-1"), base.commit)
        runs = seed(path, ["echo hi > seeded.txt", "true"])
        self.assertEqual(len(runs), 2)
        self.assertEqual((path / "seeded.txt").read_text().strip(), "hi")
        with self.assertRaises(WorkspaceError) as ctx:
            seed(path, ["echo boom; exit 3"])
        self.assertIn("3", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_integrate_applied_conflict_empty(self):
        base = take_snapshot(self.repo, "T-1", "baseline")
        path = create_worktree(self.repo, worktree_path(self.root, self.repo, "T-1"), base.commit)
        (path / "app" / "service.py").write_text(HELLO_DOC)
        final = take_snapshot(path, "T-1", "final")
        patch = patch_between(path, base, final)
        self.assertEqual(integrate(self.repo, ""), "empty")
        self.assertEqual(integrate(self.repo, patch), "applied")
        self.assertEqual((self.repo / "app" / "service.py").read_text(), HELLO_DOC)
        (self.repo / "app" / "service.py").write_text("conflicting user edit\n")
        self.assertEqual(integrate(self.repo, patch), "conflict")
        self.assertEqual((self.repo / "app" / "service.py").read_text(), "conflicting user edit\n")


class WorktreeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def run_spec(self, steps, **fields):
        fields.setdefault("acceptance", [{"cmd": GATE_CMD}])
        fields.setdefault("scope", {"write": ["app/service.py"], "forbid": ["tests/", "tools/"]})
        fields.setdefault("workspace", {"mode": "worktree"})
        fields.setdefault("budget", {"attempts_per_tier": 1, "tiers": ["m1"]})
        spec = load_spec(write_spec(self.tmp, self.repo, **fields))
        state = TaskState(self.root, self.repo, spec.id)
        executor = FakeExecutor(steps)
        return run_task(spec, executor=executor, state=state), executor, state

    def test_main_tree_untouched_until_accept_then_integrated(self):
        original = (self.repo / "app" / "service.py").read_text()
        (self.repo / "README.md").write_text("user is editing the readme\n")
        (self.repo / "notes.md").write_text("draft")
        seen = {}

        def step(prompt, work):
            seen["work"] = work
            seen["main_during"] = (self.repo / "app" / "service.py").read_text()
            (work / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())

        result, executor, state = self.run_spec([step])
        expected_work = worktree_path(self.root, self.repo, "T-1")
        self.assertEqual(seen["work"], expected_work)
        self.assertEqual(seen["main_during"], original)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["refs"]["integration"], "applied")
        self.assertEqual(result["changed_files"], ["app/service.py"])
        self.assertEqual((self.repo / "app" / "service.py").read_text(), HELLO_DOC)
        self.assertEqual((self.repo / "README.md").read_text(), "user is editing the readme\n")
        self.assertEqual((self.repo / "notes.md").read_text(), "draft")
        self.assertFalse(expected_work.exists())
        self.assertIsNone(result["refs"]["worktree"])

    def test_conflict_keeps_patch_and_worktree_then_cli_integrate(self):
        def step(prompt, work):
            (work / "app" / "service.py").write_text(HELLO_DOC)
            (self.repo / "app" / "service.py").write_text("user changed it meanwhile\n")
            return envelope(so_code())

        result, _, state = self.run_spec([step])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["refs"]["integration"], "conflict")
        self.assertEqual(result["refs"]["patch"], str(state.patch_path))
        self.assertTrue(state.patch_path.exists())
        work = worktree_path(self.root, self.repo, "T-1")
        self.assertTrue(work.exists())
        self.assertEqual(result["refs"]["worktree"], str(work))
        self.assertEqual((self.repo / "app" / "service.py").read_text(), "user changed it meanwhile\n")
        (self.repo / "app" / "service.py").write_text(git(self.repo, "show", "HEAD:app/service.py"))
        from tests.test_cli import run_cli
        code, out, _ = run_cli(["integrate", "T-1", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("applied", out)
        self.assertEqual((self.repo / "app" / "service.py").read_text(), HELLO_DOC)
        self.assertEqual(state.load_result()["refs"]["integration"], "applied")

    def test_rejected_keeps_worktree_and_clean_removes_it(self):
        def step(prompt, work):
            (work / "app" / "service.py").write_text(HELLO_ONLY)
            return envelope(so_code())

        result, _, _ = self.run_spec([step])
        self.assertEqual(result["status"], "rejected")
        work = worktree_path(self.root, self.repo, "T-1")
        self.assertTrue(work.exists())
        self.assertEqual(result["refs"]["worktree"], str(work))
        self.assertEqual((self.repo / "app" / "service.py").read_text(), git(self.repo, "show", "HEAD:app/service.py"))
        from tests.test_cli import run_cli
        code, out, _ = run_cli(["clean", "T-1", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertFalse(work.exists())

    def test_seed_failure_is_infra_failed_without_executor(self):
        result, executor, _ = self.run_spec([envelope(so_code())], workspace={"mode": "worktree", "seed": ["echo no deps; exit 7"]})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"], {"domain": "infra", "kind": "seed"})
        self.assertEqual(executor.calls, [])
        self.assertTrue(any("7" in v for v in result["checks"]["violations"]))

    def test_seed_runs_before_executor(self):
        seen = {}

        def step(prompt, work):
            seen["seeded"] = (work / "seeded.txt").exists()
            (work / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())

        result, _, _ = self.run_spec([step], workspace={"mode": "worktree", "seed": ["echo x > seeded.txt"]})
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(seen["seeded"])
        self.assertFalse((self.repo / "seeded.txt").exists())

    def test_retry_continues_in_existing_worktree(self):
        def weak(prompt, work):
            (work / "app" / "service.py").write_text(HELLO_ONLY)
            return envelope(so_code(), conversation="c9")

        result, _, state = self.run_spec([weak])
        self.assertEqual(result["status"], "rejected")
        seen = {}

        def fix(prompt, work):
            seen["work"] = work
            (work / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code(), conversation="c9")

        from tests.test_cli import run_cli
        msg = self.tmp / "fix.md"
        msg.write_text("Добавь docstring.", encoding="utf-8")
        code, out, _ = run_cli(["retry", "T-1", "--message", str(msg), "--dir", str(self.repo), "--state", str(self.root)], executor=FakeExecutor([fix]))
        self.assertEqual(code, 0)
        self.assertEqual(seen["work"], worktree_path(self.root, self.repo, "T-1"))
        self.assertEqual(state.load_result()["refs"]["integration"], "applied")
        self.assertEqual((self.repo / "app" / "service.py").read_text(), HELLO_DOC)


if __name__ == "__main__":
    unittest.main()
