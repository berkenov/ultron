import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.helpers import GIT_ENV, HELLO_DOC, HELLO_ONLY, git, make_repo
from ultron.snapshot import Snapshot, diff_paths, drop_snapshots, list_snapshots, patch_between, restore_all, restore_paths, take_snapshot

HEX40 = r"^[0-9a-f]{40}$"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_baseline_has_all_files_and_a_ref(self):
        snap = take_snapshot(self.repo, "T-1", "baseline")
        self.assertIsInstance(snap, Snapshot)
        self.assertEqual(snap.label, "baseline")
        self.assertRegex(snap.commit, HEX40)
        self.assertRegex(snap.tree, HEX40)
        self.assertIn("app/service.py", snap.files)
        self.assertIn("tools/check.py", snap.files)
        self.assertEqual(snap.modes["app/service.py"], "100644")
        self.assertEqual(git(self.repo, "rev-parse", "refs/ultron/T-1/baseline").strip(), snap.commit)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_includes_uncommitted_and_untracked_without_staging(self):
        (self.repo / "app" / "service.py").write_text(HELLO_ONLY)
        (self.repo / "notes.md").write_text("draft")
        snap = take_snapshot(self.repo, "T-1", "baseline")
        self.assertIn("notes.md", snap.files)
        head_blob = git(self.repo, "rev-parse", "HEAD:app/service.py").strip()
        self.assertNotEqual(snap.files["app/service.py"], head_blob)
        status = git(self.repo, "status", "--porcelain")
        self.assertIn("?? notes.md", status)
        self.assertIn(" M app/service.py", status)

    def test_respects_gitignore(self):
        (self.repo / ".gitignore").write_text("secret.txt\n")
        (self.repo / "secret.txt").write_text("x")
        snap = take_snapshot(self.repo, "T-1", "baseline")
        self.assertNotIn("secret.txt", snap.files)
        self.assertIn(".gitignore", snap.files)

    def test_works_in_repo_without_commits(self):
        fresh = self.tmp / "fresh"
        fresh.mkdir()
        git(fresh, "init", "-q")
        (fresh / "a.txt").write_text("a")
        snap = take_snapshot(fresh, "T-9", "baseline")
        self.assertIn("a.txt", snap.files)
        self.assertRegex(snap.commit, HEX40)

    def test_does_not_depend_on_user_identity(self):
        env = {k: v for k, v in GIT_ENV.items() if not k.startswith("GIT_AUTHOR") and not k.startswith("GIT_COMMITTER")}
        env["GIT_CONFIG_GLOBAL"] = str(self.tmp / "no-config")
        code = "import pathlib, sys; sys.path.insert(0, %r); from ultron.snapshot import take_snapshot; print(take_snapshot(pathlib.Path(%r), 'T-2', 'baseline').commit)" % (str(pathlib.Path(__file__).resolve().parents[1]), str(self.repo))
        proc = subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout.strip(), HEX40)

    def test_diff_paths_modified_added_deleted_bytecode_ignored(self):
        base = take_snapshot(self.repo, "T-1", "baseline")
        (self.repo / "app" / "service.py").write_text(HELLO_ONLY)
        (self.repo / "new.txt").write_text("n")
        (self.repo / "README.md").unlink()
        (self.repo / "app" / "__pycache__").mkdir()
        (self.repo / "app" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        cur = take_snapshot(self.repo, "T-1", "attempt-1")
        self.assertEqual(diff_paths(base, cur), ["README.md", "app/service.py", "new.txt"])
        self.assertEqual(diff_paths(base, base), [])

    def test_restore_paths_from_baseline_keeps_user_version_and_exec_bit(self):
        check = self.repo / "tools" / "check.py"
        check.chmod(check.stat().st_mode | stat.S_IXUSR)
        user_version = "# user draft\n" + (self.repo / "tests" / "test_service.py").read_text()
        (self.repo / "tests" / "test_service.py").write_text(user_version)
        base = take_snapshot(self.repo, "T-1", "baseline")
        (self.repo / "tests" / "test_service.py").write_text("broken by executor\n")
        (self.repo / "new.txt").write_text("n")
        check.write_text(check.read_text() + "\n# touched\n")
        restored = restore_paths(self.repo, base, ["tests/test_service.py", "new.txt", "tools/check.py"])
        self.assertEqual(sorted(restored), ["new.txt", "tests/test_service.py", "tools/check.py"])
        self.assertEqual((self.repo / "tests" / "test_service.py").read_text(), user_version)
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertNotIn("# touched", check.read_text())
        self.assertTrue(check.stat().st_mode & stat.S_IXUSR)

    def test_restore_all_returns_tree_to_target(self):
        base = take_snapshot(self.repo, "T-1", "baseline")
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        (self.repo / "README.md").write_text("changed")
        (self.repo / "extra.txt").write_text("e")
        cur = take_snapshot(self.repo, "T-1", "attempt-1")
        restored = restore_all(self.repo, cur, base)
        self.assertEqual(len(restored), 3)
        after = take_snapshot(self.repo, "T-1", "check")
        self.assertEqual(diff_paths(base, after), [])

    def test_patch_between(self):
        base = take_snapshot(self.repo, "T-1", "baseline")
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        cur = take_snapshot(self.repo, "T-1", "attempt-1")
        patch = patch_between(self.repo, base, cur)
        self.assertIn("--- a/app/service.py", patch)
        self.assertIn('+    return f"hello {user.name}"', patch)

    def test_list_drop_and_relabel(self):
        take_snapshot(self.repo, "T-1", "baseline")
        first = take_snapshot(self.repo, "T-1", "attempt-1")
        (self.repo / "app" / "service.py").write_text(HELLO_DOC)
        second = take_snapshot(self.repo, "T-1", "attempt-1")
        self.assertNotEqual(first.commit, second.commit)
        self.assertEqual(git(self.repo, "rev-parse", "refs/ultron/T-1/attempt-1").strip(), second.commit)
        self.assertEqual(list_snapshots(self.repo, "T-1"), ["attempt-1", "baseline"])
        drop_snapshots(self.repo, "T-1")
        self.assertEqual(list_snapshots(self.repo, "T-1"), [])
        proc = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", "refs/ultron/T-1/baseline"], env=GIT_ENV, capture_output=True)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
