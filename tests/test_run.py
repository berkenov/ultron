import json
import pathlib
import tempfile
import unittest

from tests.helpers import GATE_CMD, HELLO_DOC, HELLO_ONLY, FakeExecutor, envelope, git, make_repo, so_code, so_explore, write_spec
from ultron.run import run_task
from ultron.spec import load_spec
from ultron.state import TaskState, project_slug


def writes(path_rel: str, text: str, so: dict, conversation="conv-1"):
    def step(prompt, repo):
        (repo / path_rel).write_text(text)
        return envelope(so, conversation=conversation)
    return step


class RunTests(unittest.TestCase):
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
        spec = load_spec(write_spec(self.tmp, self.repo, **fields))
        state = TaskState(self.root, self.repo, spec.id)
        executor = FakeExecutor(steps)
        result = run_task(spec, executor=executor, state=state)
        return result, executor, state

    def test_accepted_first_try(self):
        result, executor, state = self.run_spec([writes("app/service.py", HELLO_DOC, so_code())])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["executor_status"], "done")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["tier"], "gemini-4-argon-high")
        self.assertEqual(result["changed_files"], ["app/service.py"])
        self.assertEqual(result["claimed_files"], ["app/service.py"])
        self.assertEqual(result["checks"]["contract"], "ok")
        self.assertEqual(result["checks"]["scope"], "ok")
        self.assertEqual(result["checks"]["gate"]["exit"], 0)
        self.assertEqual(result["checks"]["violations"], [])
        self.assertEqual(result["evidence"], [{"id": "e1", "where": "app/service.py:5"}])
        self.assertEqual(result["usage"]["executor_in"], 100)
        self.assertEqual(result["refs"]["conversation"], "conv-1")
        self.assertEqual(len(executor.calls), 1)
        self.assertIsNone(executor.calls[0]["conversation"])
        self.assertEqual(executor.calls[0]["model"], "gemini-4-argon-high")
        self.assertTrue(executor.calls[0]["schema"].name == "code.json")
        for p in (state.spec_path, state.brief_path, state.attempts_path, state.gate_log_path, state.result_path):
            self.assertTrue(p.exists(), p)
        journal = self.root / project_slug(self.repo) / "journal.jsonl"
        lines = [json.loads(l) for l in journal.read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["_meta"]["task"], "T-1")
        self.assertEqual(lines[0]["usage"]["input_tokens"], 100)
        self.assertEqual(state.load_result()["status"], "accepted")

    def test_retry_in_same_session_on_gate_failure(self):
        result, executor, state = self.run_spec([
            writes("app/service.py", HELLO_ONLY, so_code()),
            writes("app/service.py", HELLO_DOC, so_code()),
        ])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(executor.calls), 2)
        second = executor.calls[1]
        self.assertEqual(second["conversation"], "conv-1")
        self.assertIn("Приёмка не пройдена", second["prompt"])
        self.assertIn("CHECK FAIL", second["prompt"])
        self.assertEqual(result["checks"]["gate"]["attempt"], 2)
        self.assertEqual(len(state.load_attempts()), 2)

    def test_forbidden_change_is_restored_by_runtime(self):
        user_version = "# user draft\n" + (self.repo / "tests" / "test_service.py").read_text()
        (self.repo / "tests" / "test_service.py").write_text(user_version)

        def bad(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            (repo / "tests" / "test_service.py").write_text("broken by executor\n")
            return envelope(so_code())

        result, executor, _ = self.run_spec([bad])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["checks"]["scope"], "restored")
        self.assertEqual(result["checks"]["restored"], ["tests/test_service.py"])
        self.assertEqual(result["changed_files"], ["app/service.py"])
        self.assertEqual((self.repo / "tests" / "test_service.py").read_text(), user_version)

    def test_diff_size_limit(self):
        def over_limit(prompt, repo):
            (repo / "app" / "service.py").write_text("1\n2\n3\n4\n5\n")
            return envelope(so_code())
            
        def within_limit(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())

        result, executor, _ = self.run_spec([over_limit, within_limit], scope={"write": ["app/service.py"], "max_diff_lines": 3})
        
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 2)
        
        second_prompt = executor.calls[1]["prompt"]
        self.assertIn("Файлы восстановлены из исходного состояния. Внеси только точечные правки: не больше 3 строк diff", second_prompt)
        self.assertEqual(result["checks"]["restored"], ["app/service.py"])

    def test_restored_forbidden_file_then_gate_red_leads_to_retry(self):
        def cheat(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_ONLY)
            (repo / "tools" / "check.py").write_text("print('CHECK OK')\n")
            return envelope(so_code())

        def honest(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())

        result, executor, _ = self.run_spec([cheat, honest])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 2)
        second = executor.calls[1]["prompt"]
        self.assertIn("восстановлены оркестратором", second)
        self.assertIn("tools/check.py", second)
        self.assertIn("CHECK FAIL", second)
        self.assertEqual(result["checks"]["restored"], ["tools/check.py"])

    def test_rejected_when_attempts_exhausted(self):
        result, executor, _ = self.run_spec(
            [writes("app/service.py", HELLO_ONLY, so_code()), writes("app/service.py", HELLO_ONLY, so_code())],
            budget={"attempts_per_tier": 2, "tiers": ["m1"]},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["attempts"], 2)
        self.assertTrue(result["checks"]["violations"])
        self.assertEqual(len(executor.calls), 2)

    def test_escalates_to_next_tier_in_new_session(self):
        result, executor, _ = self.run_spec(
            [writes("app/service.py", HELLO_ONLY, so_code(summary="половина"), conversation="c1"), writes("app/service.py", HELLO_DOC, so_code(), conversation="c2")],
            budget={"attempts_per_tier": 1, "tiers": ["m1", "m2"]},
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["tier"], "m2")
        self.assertEqual(executor.calls[0]["model"], "m1")
        self.assertEqual(executor.calls[1]["model"], "m2")
        self.assertIsNone(executor.calls[1]["conversation"])
        self.assertIn("ПРЕДЫДУЩИЕ ПОПЫТКИ", executor.calls[1]["prompt"])
        self.assertIn("половина", executor.calls[1]["prompt"])
        self.assertEqual(result["refs"]["conversation"], "c2")

    def test_blocked_stops_immediately(self):
        result, executor, _ = self.run_spec([envelope(so_code(status="blocked", changed=(), open_questions=["Какой префикс?"]))])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["open_questions"], ["Какой префикс?"])
        self.assertEqual(len(executor.calls), 1)

    def test_blocked_wins_over_green_checks(self):
        def fix_but_ask(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code(status="blocked", open_questions=["Оставить hi для гостей?"]))
        result, executor, _ = self.run_spec([fix_but_ask])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"]["contract"], "ok")
        self.assertEqual(result["checks"]["gate"]["exit"], 0)
        self.assertEqual(len(executor.calls), 1)

    def test_no_output_is_failed(self):
        from ultron.executor import Envelope
        empty = Envelope(status="NO_OUTPUT", conversation_id=None, structured_output=None, denied_actions=(), usage={}, num_turns=None, duration_seconds=0.0, raw={}, noise=("agy: boom",))
        result, executor, _ = self.run_spec([empty])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["executor_status"])
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result["failure"], {"domain": "infra", "kind": "no_output"})

    def test_token_budget_exceeded_is_failed(self):
        big = {"input_tokens": 90, "output_tokens": 20, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 110}
        result, executor, _ = self.run_spec(
            [writes("app/service.py", HELLO_ONLY, so_code()), writes("app/service.py", HELLO_DOC, so_code())],
            budget={"executor_tokens": 100},
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(any("budget" in v for v in result["checks"]["violations"]))
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result["failure"], {"domain": "infra", "kind": "budget_tokens"})

    def test_readonly_role_edits_are_restored(self):
        original = (self.repo / "app" / "service.py").read_text()
        result, executor, _ = self.run_spec([writes("app/service.py", HELLO_DOC, so_explore(evidence=[{"id": "e1", "where": "-", "excerpt": ""}]))], role="explore", acceptance=[], scope={})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["checks"]["scope"], "restored")
        self.assertEqual(result["checks"]["restored"], ["app/service.py"])
        self.assertEqual((self.repo / "app" / "service.py").read_text(), original)
        self.assertEqual(result["changed_files"], [])
        self.assertIsNone(result["checks"]["gate"])

    def test_broken_report_with_green_code_asks_for_report_only(self):
        def code_ok_report_broken(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(None, response="not json")

        result, executor, _ = self.run_spec([code_ok_report_broken, envelope(so_code())])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 2)
        self.assertIn("Код принят", executor.calls[1]["prompt"])
        self.assertIn("отчёт", executor.calls[1]["prompt"].lower())
        self.assertEqual(executor.calls[1]["conversation"], "conv-1")
        self.assertEqual(result["checks"]["contract"], "ok")

    def test_report_never_comes_is_accepted_with_missing_contract(self):
        def code_ok_report_broken(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(None, response="still not json")

        result, executor, _ = self.run_spec([code_ok_report_broken, envelope(None, response="nope")])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["checks"]["contract"], "missing")
        self.assertIsNone(result["executor_status"])
        self.assertIn("отчёт", result["summary"].lower())
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(result["changed_files"], ["app/service.py"])

    def test_escalation_restores_baseline_and_saves_patch(self):
        original = (self.repo / "app" / "service.py").read_text()
        seen = {}

        def weak(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_ONLY)
            return envelope(so_code(summary="половина"), conversation="c1")

        def strong(prompt, repo):
            seen["tree_at_start"] = (repo / "app" / "service.py").read_text()
            seen["prompt"] = prompt
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code(), conversation="c2")

        result, executor, state = self.run_spec([weak, strong], budget={"attempts_per_tier": 1, "tiers": ["m1", "m2"]})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(seen["tree_at_start"], original)
        self.assertTrue(state.attempt_patch_path(1).exists())
        self.assertIn('+    return f"hello {user.name}"', state.attempt_patch_path(1).read_text())
        self.assertIn(str(state.attempt_patch_path(1)), seen["prompt"])
        self.assertIn("ПРЕДЫДУЩИЕ ПОПЫТКИ", seen["prompt"])

    def test_snapshots_recorded(self):
        result, _, state = self.run_spec([writes("app/service.py", HELLO_DOC, so_code())])
        self.assertRegex(result["refs"]["snapshots"]["baseline"], r"^[0-9a-f]{40}$")
        self.assertEqual(len(result["refs"]["snapshots"]["attempts"]), 1)
        self.assertEqual(state.load_attempts()[0]["_meta"]["snapshot"]["label"], "attempt-1")
        self.assertEqual(git(self.repo, "rev-parse", "refs/ultron/T-1/baseline").strip(), result["refs"]["snapshots"]["baseline"])
        self.assertEqual(result["failure"], {"domain": "none", "kind": ""})

    def test_failure_classes(self):
        rejected, _, _ = self.run_spec([writes("app/service.py", HELLO_ONLY, so_code())], budget={"attempts_per_tier": 1, "tiers": ["m1"]})
        self.assertEqual(rejected["failure"], {"domain": "execution", "kind": "gate"})
        blocked, _, _ = self.run_spec([envelope(so_code(status="blocked", changed=()))], id="T-2")
        self.assertEqual(blocked["failure"], {"domain": "decision", "kind": "blocked"})
        from ultron.executor import Envelope
        timed_out = Envelope(status="TIMEOUT", conversation_id=None, structured_output=None, denied_actions=(), usage={}, num_turns=None, duration_seconds=0.0, raw={}, noise=())
        failed, _, _ = self.run_spec([timed_out], id="T-3")
        self.assertEqual(failed["failure"], {"domain": "infra", "kind": "timeout"})

    def test_plan_mode_saves_patch_and_forbids_edits(self):
        patch = "--- a/app/service.py\n+++ b/app/service.py\n@@ -1 +1 @@\n-hi\n+hello\n"
        result, executor, state = self.run_spec([envelope(so_code(changed=(), patch=patch))], executor={"mode": "plan"}, acceptance=[])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["refs"]["patch"], str(state.patch_path))
        self.assertEqual(state.patch_path.read_text(), patch)
        self.assertEqual(executor.calls[0]["mode"], "plan")
        self.assertEqual(result["changed_files"], [])

    def test_usage_summed_and_role_fields_kept(self):
        result, _, _ = self.run_spec([
            writes("app/service.py", HELLO_ONLY, so_code()),
            writes("app/service.py", HELLO_DOC, so_code(commands=((GATE_CMD, 0, "зелёный"),))),
        ])
        self.assertEqual(result["usage"]["executor_in"], 200)
        self.assertEqual(result["usage"]["executor_out"], 20)
        self.assertEqual(result["usage"]["cache_read"], 40)
        self.assertEqual(result["role_fields"]["commands"][0]["summary"], "зелёный")
        self.assertNotIn("summary", result["role_fields"])
        self.assertNotIn("status", result["role_fields"])

    def test_inputs_are_quoted_into_prompt(self):
        prev = TaskState(self.root, self.repo, "T-0")
        prev.save_result({"id": "T-0", "status": "accepted", "summary": "Карта из прошлой задачи.", "role_fields": {"map": [{"path": "app/x.py", "purpose": "иксы", "depends_on": []}]}})
        result, executor, _ = self.run_spec([writes("app/service.py", HELLO_DOC, so_code())], inputs=[{"task": "T-0", "fields": ["summary", "map"]}])
        self.assertEqual(result["status"], "accepted")
        self.assertIn("ДАНО ИЗ T-0", executor.calls[0]["prompt"])
        self.assertIn("Карта из прошлой задачи.", executor.calls[0]["prompt"])
        self.assertIn("app/x.py", executor.calls[0]["prompt"])

    def test_brief_saved_is_what_was_sent(self):
        _, executor, state = self.run_spec([writes("app/service.py", HELLO_DOC, so_code())])
        self.assertEqual(state.brief_path.read_text(encoding="utf-8"), executor.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
