import pathlib
import tempfile
import unittest

from tests.helpers import FakeExecutor, envelope, make_repo
from tests.test_cli import run_cli
from ultron.askcheck import check_answer
from ultron.usage import render, summarize


class Base(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.repo = make_repo(self.tmp)
        self.root = self.tmp / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def cli(self, *argv, steps=()):
        ex = FakeExecutor(list(steps))
        code, out, _ = run_cli([*argv, "--state", str(self.root)], executor=ex)
        return code, out, ex


class CheckAnswerTests(Base):
    def test_true_references_pass(self):
        r = check_answer("`greet` в app/service.py:4 возвращает строку, тип `User` из `app.models`.", self.repo)
        self.assertEqual((r.anchors, r.problems), (1, ()))

    def test_missing_file_line_and_name_are_reported(self):
        text = f"См. app/nope.py:3, app/service.py:999 и [ссылку](file://{self.repo}/app/service.py#L2). Функция `say_hello()` там же, слово `привет` не проверяется."
        r = check_answer(text, self.repo)
        self.assertEqual(r.anchors, 3)
        self.assertEqual(len(r.problems), 3)
        self.assertTrue(any("файла нет: app/nope.py" in p for p in r.problems))
        self.assertTrue(any("строки нет: app/service.py:999" in p for p in r.problems))
        self.assertTrue(any("`say_hello()`" in p for p in r.problems))

    def test_names_are_not_judged_without_anchored_files(self):
        self.assertEqual(check_answer("Есть `some_function`, но где — не знаю.", self.repo).problems, ())


class AskCliTests(Base):
    def test_unverified_places_are_shown(self):
        code, out, _ = self.cli("ask", "вопрос", "--dir", str(self.repo), steps=[envelope(response="`make_greeting` в app/service.py:4")])
        self.assertEqual(code, 0)
        self.assertIn("НЕ ПОДТВЕРЖДЕНО (1)", out)

    def test_verified_answer_says_what_was_checked(self):
        _, out, _ = self.cli("ask", "вопрос", "--dir", str(self.repo), steps=[envelope(response="`greet` в app/service.py:4")])
        self.assertIn("сверено с кодом: якорей 1, имён 1", out)

    def test_small_files_are_not_sent_unless_forced(self):
        code, out, ex = self.cli("ask", "вопрос", "--dir", str(self.repo), "--file", "app/service.py")
        self.assertEqual((code, ex.calls), (3, []))
        self.assertIn("прочитать самому дешевле", out)
        code, _, ex = self.cli("ask", "вопрос", "--dir", str(self.repo), "--file", "app/service.py", "--force", steps=[envelope(response="ок")])
        self.assertEqual((code, len(ex.calls)), (0, 1))


class NoteAndUsageTests(Base):
    def test_notes_are_decisions_not_calls(self):
        self.assertEqual(self.cli("note", "фильтр по дате", "--to", "self", "--why", "контекст в разговоре", "--dir", str(self.repo))[0], 0)
        self.assertEqual(self.cli("note", "сводка диффа", "--to", "flash", "--dir", str(self.repo))[0], 0)
        self.assertEqual(self.cli("note", "x", "--to", "nobody", "--dir", str(self.repo))[0], 1)
        self.cli("ask", "длинный вопрос", "--dir", str(self.repo), steps=[envelope(response="`nope_name` в app/service.py:4")])
        s = summarize(self.root)
        self.assertEqual(s["decisions"], {"self": 1, "flash": 1})
        self.assertEqual(sum(r["calls"] for r in s["windows"]["5 ч"].values()), 1)
        self.assertEqual((s["outcomes"], s["cost"]["unverified"]), ({"answered": 1}, 1))
        self.assertGreater(s["cost"]["claude_chars"], 0)
        text = render(s)
        self.assertIn("Решения за 7 дн", text)
        self.assertIn("Неподтверждённых мест", text)
