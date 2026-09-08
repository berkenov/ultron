import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ultron import policy
from ultron.state import TaskState, list_tasks, project_slug, state_root


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.project = self.tmp / "repo"
        self.project.mkdir()
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_project_slug(self):
        self.assertEqual(project_slug(pathlib.Path("/Users/x/dev/ultron")), "-Users-x-dev-ultron")

    def test_state_root_env_and_default(self):
        with mock.patch.dict(os.environ, {policy.STATE_ROOT_ENV: str(self.root)}):
            self.assertEqual(state_root(), self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(policy.STATE_ROOT_ENV, None)
            self.assertEqual(state_root(), pathlib.Path(policy.DEFAULT_STATE_ROOT).expanduser())

    def test_paths_under_root_slug_tasks(self):
        st = TaskState(self.root, self.project, "T-1")
        self.assertEqual(st.dir, self.root / project_slug(self.project) / "tasks" / "T-1")
        self.assertEqual(st.spec_path.name, "spec.json")
        self.assertEqual(st.brief_path.name, "brief.md")
        self.assertEqual(st.attempts_path.name, "attempts.jsonl")
        self.assertEqual(st.gate_log_path.name, "gate.log")
        self.assertEqual(st.patch_path.name, "patch.diff")
        self.assertEqual(st.result_path.name, "result.json")
        self.assertFalse(st.dir.exists())

    def test_round_trips(self):
        st = TaskState(self.root, self.project, "T-1")
        self.assertIsNone(st.load_spec())
        self.assertIsNone(st.load_result())
        self.assertEqual(st.load_attempts(), [])
        st.save_spec({"id": "T-1", "goal": "цель"})
        st.save_brief("бриф")
        st.append_attempt({"n": 1, "text": "первая"})
        st.append_attempt({"n": 2})
        st.save_gate_log("PASS\nFAIL\n")
        patch = st.save_patch("--- a\n+++ b\n")
        st.save_result({"id": "T-1", "status": "accepted"})
        self.assertEqual(st.load_spec()["goal"], "цель")
        self.assertEqual(st.brief_path.read_text(encoding="utf-8"), "бриф")
        self.assertEqual([a["n"] for a in st.load_attempts()], [1, 2])
        self.assertIn("первая", st.attempts_path.read_text(encoding="utf-8"))
        self.assertEqual(st.gate_log_path.read_text(), "PASS\nFAIL\n")
        self.assertEqual(patch, st.patch_path)
        self.assertEqual(patch.read_text(), "--- a\n+++ b\n")
        self.assertEqual(st.load_result()["status"], "accepted")

    def test_attempt_patches_and_reset(self):
        st = TaskState(self.root, self.project, "T-1")
        self.assertEqual(st.attempt_patch_path(2).name, "attempt-2.diff")
        p = st.save_attempt_patch(2, "--- a\n+++ b\n")
        self.assertEqual(p, st.attempt_patch_path(2))
        st.append_attempt({"n": 1})
        st.save_result({"id": "T-1"})
        st.reset()
        self.assertFalse(p.exists())
        self.assertEqual(st.load_attempts(), [])
        self.assertIsNone(st.load_result())
        self.assertTrue(st.dir.exists())

    def test_journal_is_per_project(self):
        st = TaskState(self.root, self.project, "T-1")
        st.journal_append({"task": "T-1", "attempt": 1})
        st.journal_append({"task": "T-1", "attempt": 2})
        journal = self.root / project_slug(self.project) / "journal.jsonl"
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["attempt"] for line in lines], [1, 2])

    def test_list_tasks(self):
        self.assertEqual(list_tasks(self.root, self.project), [])
        TaskState(self.root, self.project, "T-2").save_spec({"id": "T-2"})
        TaskState(self.root, self.project, "T-1").save_spec({"id": "T-1"})
        self.assertEqual(list_tasks(self.root, self.project), ["T-1", "T-2"])


if __name__ == "__main__":
    unittest.main()
