import pathlib
import tempfile
import unittest

from tests.helpers import make_repo, result_dict, write_spec
from ultron import policy
from ultron.check import CheckReport, GateRun
from ultron.compose import AttemptSummary, compose_prompt, escalation_prompt, report_only_message, retry_message
from ultron.spec import load_spec

BRIEF = "Роль: code\nКОНТЕКСТ — учебный проект.\nЗАДАЧА — заменить hi на hello.\n"


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        self.tmpdir.cleanup()

    def spec(self, **fields):
        return load_spec(write_spec(self.tmp, self.repo, brief=BRIEF, **fields))

    def test_prompt_starts_with_brief_and_ends_with_git_ban(self):
        prompt = compose_prompt(self.spec(), BRIEF, {})
        self.assertTrue(prompt.startswith(BRIEF.rstrip()))
        self.assertTrue(prompt.rstrip().endswith(policy.GIT_BAN))
        self.assertEqual(prompt.count(policy.GIT_BAN), 1)

    def test_code_role_scope_lines(self):
        spec = self.spec(scope={"write": ["app/service.py"], "forbid": ["tests/", "tools/"]})
        prompt = compose_prompt(spec, BRIEF, {})
        self.assertIn("ОБЪЁМ", prompt)
        self.assertIn("app/service.py", prompt)
        self.assertIn("tests/", prompt)
        self.assertIn("tools/", prompt)
        self.assertNotIn(policy.READONLY_BAN, prompt)

    def test_empty_write_means_any_file(self):
        prompt = compose_prompt(self.spec(), BRIEF, {})
        self.assertIn("любые файлы", prompt)

    def test_readonly_role_gets_readonly_ban(self):
        spec = self.spec(role="explore", id="T-2")
        prompt = compose_prompt(spec, BRIEF, {})
        self.assertIn(policy.READONLY_BAN, prompt)
        self.assertIn(policy.ROLES["explore"].answer_template, prompt)
        self.assertNotIn(policy.GATE_NOTE, prompt)

    def test_plan_mode_is_readonly(self):
        spec = self.spec(id="T-3", executor={"mode": "plan"})
        self.assertIn(policy.READONLY_BAN, compose_prompt(spec, BRIEF, {}))

    def test_answer_template_once_and_gate_note_with_acceptance(self):
        spec = self.spec(id="T-4", acceptance=[{"cmd": "true"}])
        prompt = compose_prompt(spec, BRIEF, {})
        self.assertEqual(prompt.count(policy.ROLES["code"].answer_template), 1)
        self.assertIn(policy.GATE_NOTE, prompt)

    def test_no_gate_note_without_acceptance(self):
        self.assertNotIn(policy.GATE_NOTE, compose_prompt(self.spec(id="T-5"), BRIEF, {}))

    def test_inputs_quoted_under_dano_block(self):
        spec = self.spec(id="T-6", inputs=[{"task": "T-01", "fields": ["summary", "map"]}, {"task": "T-02"}])
        t01 = result_dict(id="T-01", summary="Карта готова.", role_fields={"map": [{"path": "app/x.py", "purpose": "иксы", "depends_on": []}]})
        t02 = result_dict(id="T-02", summary="Проверено.")
        prompt = compose_prompt(spec, BRIEF, {"T-01": t01, "T-02": t02})
        self.assertIn("ДАНО ИЗ T-01", prompt)
        self.assertIn("Карта готова.", prompt)
        self.assertIn("app/x.py", prompt)
        self.assertIn("ДАНО ИЗ T-02", prompt)
        self.assertIn("Проверено.", prompt)
        self.assertLess(prompt.index("ДАНО ИЗ T-01"), prompt.index("ДАНО ИЗ T-02"))
        self.assertLess(prompt.index(BRIEF.rstrip()), prompt.index("ДАНО ИЗ T-01"))

    def test_missing_input_field_is_marked(self):
        spec = self.spec(id="T-7", inputs=[{"task": "T-01", "fields": ["nope"]}])
        prompt = compose_prompt(spec, BRIEF, {"T-01": result_dict(id="T-01")})
        self.assertIn("nope: нет", prompt)

    def test_retry_message_lists_every_cause(self):
        spec = self.spec(id="T-8", scope={"write": ["app/service.py"], "forbid": ["tests/"]})
        report = CheckReport(
            contract=("нет обязательных полей: not_done",),
            scope=("вне разрешённого объёма: tests/test_service.py",),
            gate=(GateRun(cmd="python3 tools/check.py", exit_code=1, expected=0, tail=("PASS test_greet_uses_name", "CHECK FAIL: greet has no docstring"), log="..."),),
            anchors=(),
            warnings=(),
        )
        msg = retry_message(report, spec)
        self.assertTrue(msg.startswith("Приёмка не пройдена."))
        self.assertIn("python3 tools/check.py", msg)
        self.assertIn("код выхода 1", msg)
        self.assertIn("CHECK FAIL: greet has no docstring", msg)
        self.assertIn("вне разрешённого объёма", msg)
        self.assertIn("tests/test_service.py", msg)
        self.assertIn("откати", msg)
        self.assertIn("не по схеме", msg)
        self.assertIn("not_done", msg)
        self.assertIn("app/service.py", msg)
        self.assertIn("tests/", msg)
        self.assertIn(policy.GIT_BAN, msg)
        self.assertIn("все тронутые файлы за все попытки", msg)

    def test_retry_message_omits_absent_causes(self):
        spec = self.spec(id="T-9")
        report = CheckReport(contract=(), scope=(), gate=(GateRun(cmd="true", exit_code=2, expected=0, tail=("boom",), log="boom"),), anchors=(), warnings=())
        msg = retry_message(report, spec)
        self.assertNotIn("объёма", msg)
        self.assertNotIn("Ответ не по схеме", msg)
        self.assertIn("boom", msg)

    def test_retry_message_keeps_scope_violation_text_verbatim(self):
        spec = self.spec(id="T-11", role="explore")
        report = CheckReport(contract=(), scope=("роль explore без записи, но изменено: app/service.py",), gate=(), anchors=(), warnings=())
        msg = retry_message(report, spec)
        self.assertIn("роль explore без записи, но изменено: app/service.py", msg)
        self.assertIn("откати", msg)
        self.assertIn(policy.READONLY_BAN, msg)

    def test_retry_message_restored_section(self):
        spec = self.spec(id="T-12", scope={"write": ["app/service.py"], "forbid": ["tools/"]})
        report = CheckReport(contract=(), scope=(), gate=(GateRun(cmd="x", exit_code=1, expected=0, tail=("CHECK FAIL",), log=""),), anchors=(), warnings=())
        msg = retry_message(report, spec, restored=["tools/check.py"])
        self.assertIn("восстановлены оркестратором", msg)
        self.assertIn("tools/check.py", msg)
        self.assertNotIn("откати", msg)
        self.assertIn("CHECK FAIL", msg)

    def test_report_only_message(self):
        msg = report_only_message(self.spec(id="T-13"))
        self.assertIn("Код принят", msg)
        self.assertIn("отчёт", msg.lower())
        self.assertIn("файлы не меняй", msg)
        self.assertIn(policy.GIT_BAN, msg)

    def test_escalation_prompt_mentions_previous_patch(self):
        spec = self.spec(id="T-14")
        prev = [AttemptSummary(number=1, model="m1", summary="s", violations=(), gate_tail=())]
        prompt = escalation_prompt(spec, BRIEF, {}, prev, previous_patch=pathlib.Path("/state/attempt-1.diff"))
        self.assertIn("/state/attempt-1.diff", prompt)
        self.assertIn("исходному состоянию", prompt)

    def test_escalation_prompt_lists_previous_attempts(self):
        spec = self.spec(id="T-10")
        previous = [
            AttemptSummary(number=1, model="m1", summary="сделал половину", violations=("гейт true: код 1, ожидался 0",), gate_tail=("FAIL x",)),
            AttemptSummary(number=2, model="m1", summary="снова", violations=("гейт true: код 1, ожидался 0",), gate_tail=()),
        ]
        prompt = escalation_prompt(spec, BRIEF, {}, previous)
        self.assertTrue(prompt.startswith(BRIEF.rstrip()))
        self.assertIn("ПРЕДЫДУЩИЕ ПОПЫТКИ", prompt)
        self.assertIn("#1 (m1): сделал половину", prompt)
        self.assertIn("#2 (m1): снова", prompt)
        self.assertIn("FAIL x", prompt)
        self.assertIn(policy.GIT_BAN, prompt)


if __name__ == "__main__":
    unittest.main()

    def test_retry_message_anchors(self):
        spec = self.spec(id="T-20")
        report = CheckReport(
            contract=(),
            scope=(),
            gate=(),
            anchors=("якорь: файла нет: missing.py:10", "якорь: строка 500 за пределами файла (10 строк): app/service.py:500"),
            warnings=()
        )
        msg = retry_message(report, spec)
        self.assertIn("Якоря не подтвердились:", msg)
        self.assertIn("якорь: файла нет: missing.py:10", msg)
        self.assertIn("якорь: строка 500 за пределами файла (10 строк): app/service.py:500", msg)
        self.assertIn("Для каждого: исправь путь и номер строки по файлу или сними утверждение. Выдумывать якорь нельзя; если места в коде нет — пиши «не найдено».", msg)

    def test_retry_message_anchors_readonly(self):
        spec = self.spec(id="T-21", role="explore")
        report = CheckReport(
            contract=(),
            scope=(),
            gate=(),
            anchors=("якорь: файла нет: x.py",),
            warnings=()
        )
        msg = retry_message(report, spec)
        self.assertIn("Якоря не подтвердились:", msg)
        self.assertIn("Файлы не менять, поправь ответ.", msg)

