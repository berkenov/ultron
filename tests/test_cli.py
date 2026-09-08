import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from tests.helpers import GATE_CMD, HELLO_DOC, FakeExecutor, envelope, make_repo, result_dict, so_code, write_spec
from ultron import cli
from ultron.state import TaskState


def run_cli(argv, executor=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, executor=executor)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"
        self.spec = write_spec(self.tmp, self.repo, acceptance=[{"cmd": GATE_CMD}], scope={"write": ["app/service.py"]}, budget={"tiers": ["m1"]})

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_dry_run_prints_agy_command_without_running(self):
        code, out, _ = run_cli(["run", str(self.spec), "--state", str(self.root), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("agy", out)
        self.assertIn("--dangerously-skip-permissions", out)
        self.assertIn("--json-schema", out)
        self.assertIn("<бриф", out)
        self.assertFalse((self.root).exists())

    def test_run_prints_compact_and_exit_codes(self):
        def fix(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())
        code, out, _ = run_cli(["run", str(self.spec), "--state", str(self.root)], executor=FakeExecutor([fix]))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("=== T-1 | code | accepted"))
        self.assertNotIn('"structured_output"', out)
        code2, out2, _ = run_cli(["run", str(self.spec), "--state", str(self.root)], executor=FakeExecutor([envelope(so_code(status="blocked", changed=()))]))
        self.assertEqual(code2, 2)
        self.assertIn("blocked", out2.splitlines()[0])

    def test_show_variants(self):
        st = TaskState(self.root, self.repo, "T-7")
        st.save_spec({"id": "T-7", "role": "code", "dir": str(self.repo), "brief": "b.md"})
        st.save_brief("текст брифа")
        st.append_attempt({"status": "SUCCESS", "structured_output": so_code(evidence=[{"id": "e2", "where": "app/service.py:5", "excerpt": "выдержка"}])})
        st.save_gate_log("PASS a\nCHECK OK\n")
        st.save_result(result_dict(id="T-7", refs={"state": str(st.dir), "conversation": "c", "patch": None}))
        base = ["show", "T-7", "--dir", str(self.repo), "--state", str(self.root)]
        code, out, _ = run_cli(base)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("=== T-7 | code | accepted"))
        self.assertEqual(run_cli(base + ["--evidence", "e2"])[1].strip(), "e2 @ app/service.py:5\nвыдержка")
        self.assertIn("CHECK OK", run_cli(base + ["--gate"])[1])
        self.assertEqual(run_cli(base + ["--brief"])[1].strip(), "текст брифа")
        self.assertEqual(json.loads(run_cli(base + ["--raw"])[1])["id"], "T-7")
        code, out, err = run_cli(["show", "T-404", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("T-404", out + err)

    def test_show_diff_uses_git(self):
        def fix(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())
        run_cli(["run", str(self.spec), "--state", str(self.root)], executor=FakeExecutor([fix]))
        code, out, _ = run_cli(["show", "T-1", "--dir", str(self.repo), "--state", str(self.root), "--diff"])
        self.assertEqual(code, 0)
        self.assertIn("app/service.py", out)
        self.assertIn("+", out)

    def test_list(self):
        TaskState(self.root, self.repo, "T-2").save_spec({"id": "T-2"})
        TaskState(self.root, self.repo, "T-1").save_spec({"id": "T-1"})
        code, out, _ = run_cli(["list", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["T-1", "T-2"])

    def test_retry_continues_last_session_and_rechecks(self):
        def half(prompt, repo):
            (repo / "app" / "service.py").write_text('from app.models import User\n\n\ndef greet(user: User) -> str:\n    return f"hello {user.name}"\n')
            return envelope(so_code(), conversation="c9")
        first = FakeExecutor([half, half])
        code, out, _ = run_cli(["run", str(self.spec), "--state", str(self.root)], executor=first)
        self.assertEqual(code, 2)
        self.assertIn("rejected", out.splitlines()[0])
        msg = self.tmp / "fix.md"
        msg.write_text("Добавь docstring к greet.", encoding="utf-8")

        def fix(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code(), conversation="c9")
        second = FakeExecutor([fix])
        code, out, _ = run_cli(["retry", "T-1", "--message", str(msg), "--dir", str(self.repo), "--state", str(self.root)], executor=second)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("=== T-1 | code | accepted"))
        self.assertEqual(second.calls[0]["conversation"], "c9")
        self.assertIn("Добавь docstring", second.calls[0]["prompt"])
        st = TaskState(self.root, self.repo, "T-1")
        self.assertEqual(st.load_result()["status"], "accepted")
        self.assertEqual(st.load_result()["attempts"], 3)

    def test_rollback_to_baseline(self):
        original = (self.repo / "app" / "service.py").read_text()

        def fix(prompt, repo):
            (repo / "app" / "service.py").write_text(HELLO_DOC)
            return envelope(so_code())
        run_cli(["run", str(self.spec), "--state", str(self.root)], executor=FakeExecutor([fix]))
        self.assertEqual((self.repo / "app" / "service.py").read_text(), HELLO_DOC)
        code, out, _ = run_cli(["rollback", "T-1", "--to", "baseline", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("app/service.py", out)
        self.assertEqual((self.repo / "app" / "service.py").read_text(), original)
        code, out, err = run_cli(["rollback", "T-1", "--to", "attempt-9", "--dir", str(self.repo), "--state", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("attempt-9", out + err)

    def test_main_module_and_bin_shim_exist(self):
        root = pathlib.Path(cli.__file__).resolve().parents[1]
        self.assertTrue((root / "ultron" / "__main__.py").exists())
        shim = root / "bin" / "ultron"
        self.assertTrue(shim.exists())
        self.assertTrue(shim.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
