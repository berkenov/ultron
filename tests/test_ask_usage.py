import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import FakeExecutor, envelope, make_repo
from tests.test_cli import run_cli
from ultron import pace, policy
from ultron.ask import ask, compose_question
from ultron.executor import AgyExecutor
from ultron.state import journal_append, project_slug
from ultron.usage import summarize


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()


class AskTests(Base):
    def test_question_carries_readonly_ban_and_files(self):
        text = compose_question("что делает greet?", [pathlib.Path("app/service.py")])
        self.assertIn(policy.READONLY_BAN, text)
        self.assertIn("ВОПРОС — что делает greet?", text)
        self.assertIn("- app/service.py", text)

    def test_answer_without_schema_on_cheap_model_and_journaled(self):
        ex = FakeExecutor([envelope(response="Здоровается. app/service.py:4")])
        res = ask("что делает greet?", dir=self.repo, executor=ex, root=self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.text, "Здоровается. app/service.py:4")
        self.assertEqual(res.changed, ())
        self.assertEqual(ex.calls[0]["model"], policy.ASK_MODEL)
        self.assertIsNone(ex.raw_schema[0])
        line = (self.root / project_slug(self.repo) / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        meta = json.loads(line)["_meta"]
        self.assertEqual((meta["task"], meta["verdict"], meta["model"]), ("ask", "answered", policy.ASK_MODEL))
        self.assertIn("at", meta)

    def test_file_change_is_reported(self):
        def edits(prompt, directory):
            (directory / "app" / "service.py").write_text("broken\n", encoding="utf-8")
            return envelope(response="готово")

        res = ask("вопрос", dir=self.repo, executor=FakeExecutor([edits]), root=self.root)
        self.assertEqual(len(res.changed), 1)
        self.assertIn("app/service.py", res.changed[0])

    def test_empty_answer_fails_with_exit_2(self):
        code, out, _ = run_cli(["ask", "вопрос", "--dir", str(self.repo), "--state", str(self.root)], executor=FakeExecutor([envelope(status="TIMEOUT", conversation=None)]))
        self.assertEqual(code, 2)
        self.assertIn("Ответа нет: TIMEOUT", out)

    def test_long_answer_is_capped_and_saved(self):
        long = "я" * (policy.ASK_MAX_CHARS + 500)
        code, out, _ = run_cli(["ask", "вопрос", "--dir", str(self.repo), "--state", str(self.root)], executor=FakeExecutor([envelope(response=long)]))
        self.assertEqual(code, 0)
        self.assertIn("обрезано, полный ответ:", out)
        saved = list((self.root / project_slug(self.repo) / "asks").glob("*.md"))
        self.assertEqual(saved[0].read_text(encoding="utf-8"), long)

    def test_agy_command_omits_schema_flag_when_none(self):
        cmd = AgyExecutor().command("p", dir=self.repo, model="m", schema=None)
        self.assertNotIn("--json-schema", cmd)


class UsageTests(Base):
    def _record(self, model, hours_ago, now):
        at = (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        journal_append(self.root, self.repo, {"usage": {"input_tokens": 100, "output_tokens": 10}, "_meta": {"model": model, "at": at}})

    def test_windows_split_by_age_and_model(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        self._record("flash", 1, now)
        self._record("flash", 2, now)
        self._record("argon", 30, now)
        self._record("argon", 24 * 8, now)
        journal = self.root / project_slug(self.repo) / "journal.jsonl"
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"usage": {}, "_meta": {"model": "old"}}) + "\nnot json\n")
        s = summarize(self.root, now)
        self.assertEqual(s["windows"]["5 ч"], {"flash": {"calls": 2, "input": 200, "output": 20}})
        self.assertEqual(s["windows"]["7 дн"]["argon"]["calls"], 1)
        self.assertEqual(s["windows"]["7 дн"]["flash"]["calls"], 2)
        self.assertEqual(s["undated"], 1)

    def test_cli_usage_on_empty_state(self):
        code, out, _ = run_cli(["usage", "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("За 5 ч: вызовов 0", out)


class PaceTests(Base):
    def test_waits_only_the_remaining_part(self):
        slept = []
        with mock.patch.dict("os.environ", {policy.PACE_ENV: "5"}):
            self.assertEqual(pace.wait_turn(self.root, now=lambda: 100.0, sleep=slept.append), 0.0)
            pace.wait_turn(self.root, now=lambda: 102.0, sleep=slept.append)
            pace.wait_turn(self.root, now=lambda: 200.0, sleep=slept.append)
        self.assertEqual(slept, [3.0])

    def test_zero_disables_and_writes_nothing(self):
        with mock.patch.dict("os.environ", {policy.PACE_ENV: "0"}):
            pace.wait_turn(self.root, sleep=lambda s: self.fail("slept"))
        self.assertFalse(self.root.exists())
