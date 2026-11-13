import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import GATE_CMD, FakeExecutor, envelope, make_repo, so_code, write_spec
from tests.test_cli import run_cli
from ultron import limits, policy
from ultron.executor import Envelope
from ultron.spec import load_spec
from ultron.run import run_task
from ultron.state import TaskState, journal_append
from ultron.view import compact

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
FLASH = "gemini-3.8-flash-high"


def refusal(text="RESOURCE_EXHAUSTED: quota exceeded for this model"):
    return Envelope(status="ERROR", conversation_id=None, structured_output=None, denied_actions=(), usage={}, num_turns=0, duration_seconds=1.0, raw={"status": "ERROR", "error": text}, noise=())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def calls(self, n, at, model=FLASH):
        for _ in range(n):
            journal_append(self.root, self.repo, {"usage": {"total_tokens": 1000}, "_meta": {"model": model, "at": at.isoformat(timespec="seconds")}})


class DetectionTests(unittest.TestCase):
    def test_quota_words_only_on_failed_calls(self):
        self.assertTrue(limits.is_quota_failure(refusal()))
        self.assertTrue(limits.is_quota_failure(refusal("HTTP 429 Too Many Requests")))
        self.assertFalse(limits.is_quota_failure(refusal("The stream was interrupted.")))
        self.assertFalse(limits.is_quota_failure(envelope(response="про quota и rate limit")))

    def test_family_drops_effort(self):
        self.assertEqual(limits.family("gemini-4-argon-high"), "gemini-4-argon")
        self.assertEqual(limits.family(FLASH), limits.family("gemini-3.8-flash-medium"))


class LearningTests(Base):
    def test_nothing_enforced_before_first_failure(self):
        self.calls(500, T0)
        b = limits.budget(self.root, FLASH, T0)
        self.assertEqual((b.mode, b.limits), ("free", {}))

    def test_short_window_learned_when_first_probe_succeeds(self):
        self.calls(50, T0)
        self.assertTrue(limits.record(self.root, FLASH, refusal(), T0))
        blocked = limits.budget(self.root, FLASH, T0 + timedelta(hours=1))
        self.assertEqual(blocked.mode, "empty")
        self.assertFalse(limits.budget(self.root, "gemini-4-argon-high", T0).mode == "empty")

        later = T0 + timedelta(hours=6)
        self.assertTrue(limits.budget(self.root, FLASH, later).allowed)
        self.assertFalse(limits.record(self.root, FLASH, envelope(response="ок"), later))
        b = limits.budget(self.root, FLASH, later)
        self.assertEqual(b.limits["5 ч"]["calls"], 40)  # 80% of the 50 it failed at
        self.assertNotIn("7 дн", b.limits)

        self.calls(20, later)
        self.assertEqual(limits.budget(self.root, FLASH, later).mode, "saving")
        self.calls(20, later)
        self.assertEqual(limits.budget(self.root, FLASH, later).mode, "empty")

    def test_long_window_learned_when_probe_fails_again(self):
        self.calls(200, T0)
        limits.record(self.root, FLASH, refusal(), T0)
        second = T0 + timedelta(hours=6)
        limits.record(self.root, FLASH, refusal(), second)
        entry = limits.load(self.root)[limits.family(FLASH)]
        self.assertEqual(entry["failures"][0]["window"], "7 дн")
        self.assertEqual(len(entry["failures"]), 1)
        until = datetime.fromisoformat(entry["blocked_until"])
        self.assertEqual(until - second, timedelta(hours=10))  # block doubles

    def test_threshold_creeps_up_and_ceiling_drops_once_exceeded(self):
        self.calls(50, T0)
        limits.record(self.root, FLASH, refusal(), T0)
        limits.record(self.root, FLASH, envelope(response="ок"), T0 + timedelta(hours=6))
        day10 = T0 + timedelta(days=10)
        self.assertEqual(limits.budget(self.root, FLASH, day10).limits["5 ч"]["calls"], 50)  # 0.8 + 10 * 0.02
        self.calls(55, day10)
        limits.record(self.root, FLASH, envelope(response="ок"), day10)
        self.assertNotIn("5 ч", limits.budget(self.root, FLASH, day10).limits)  # the limit was raised


class WithoutExecutorTests(Base):
    def test_ask_does_not_call_when_blocked(self):
        limits.record(self.root, policy.ASK_MODEL, refusal())
        ex = FakeExecutor([])
        code, out, _ = run_cli(["ask", "вопрос", "--dir", str(self.repo), "--state", str(self.root)], executor=ex)
        self.assertEqual((code, ex.calls), (2, []))
        self.assertIn("сделай сам", out)

    def test_run_hands_off_with_patch_when_quota_ends_midway(self):
        def half_done(prompt, directory):
            (directory / "app" / "service.py").write_text("half\n", encoding="utf-8")
            return refusal()

        spec = load_spec(write_spec(self.tmp, self.repo, acceptance=[{"cmd": GATE_CMD}], scope={"write": ["app/service.py"]}, budget={"tiers": ["m1-high"]}))
        res = run_task(spec, executor=FakeExecutor([half_done]), state=TaskState(self.root, spec.dir, spec.id))
        self.assertEqual((res["status"], res["failure"]), ("failed", {"domain": "quota", "kind": "handoff"}))
        self.assertIn("half", pathlib.Path(res["handoff"]["patch"]).read_text(encoding="utf-8"))
        self.assertIn("handoff:", compact(res))
        self.assertTrue((self.root / "quota-failures").exists())

    def test_run_skips_blocked_tier_and_fails_cleanly_when_all_blocked(self):
        limits.record(self.root, "m1-high", refusal())
        spec_path = write_spec(self.tmp, self.repo, id="T-2", scope={"write": ["app/service.py"]}, budget={"tiers": ["m1-high", "m2-high"]})
        spec = load_spec(spec_path)

        def writes(prompt, directory):
            (directory / "app" / "service.py").write_text((directory / "app" / "service.py").read_text() + "\n", encoding="utf-8")
            return envelope(so_code())

        ex = FakeExecutor([writes])
        run_task(spec, executor=ex, state=TaskState(self.root, spec.dir, spec.id))
        self.assertEqual(ex.calls[0]["model"], "m2-high")

        limits.record(self.root, "m2-high", refusal())
        spec3 = load_spec(write_spec(self.tmp, self.repo, id="T-3", budget={"tiers": ["m1-high", "m2-high"]}))
        ex3 = FakeExecutor([])
        res = run_task(spec3, executor=ex3, state=TaskState(self.root, spec3.dir, spec3.id))
        self.assertEqual((ex3.calls, res["failure"]["domain"], res["handoff"]["patch"]), ([], "quota", None))

    def test_budget_command(self):
        code, out, _ = run_cli(["budget", "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("gemini-4-argon: свободно", out)
