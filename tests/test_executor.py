import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from ultron import policy
from ultron.executor import AgyExecutor, Envelope, parse_timeout

REAL_OK = ('{"conversation_id":"57c4a29e-c756-4332-9b5d-83d362e04d0b","status":"SUCCESS",'
           '"response":"{\\"first_line\\":\\"alpha\\"}\\n","duration_seconds":7.276747,"num_turns":1,'
           '"structured_output":{"first_line":"alpha","command_ran":true},"json_schema":{"type":"object"},'
           '"usage":{"input_tokens":43021,"output_tokens":855,"thinking_tokens":423,"cache_read_tokens":16251,"total_tokens":43876}}')
REAL_DENIED = ('{"conversation_id":"27456358-1733-496f-944c-d3176d8153b9","status":"SUCCESS","response":"",'
               '"duration_seconds":18.064892,"num_turns":1,"json_schema":{"type":"object"},'
               '"usage":{"input_tokens":31347,"output_tokens":778,"thinking_tokens":425,"cache_read_tokens":60792,"total_tokens":32125},'
               '"denied_actions":[{"action":"command","display_name":"RunCommand"}]}')


class EnvelopeTests(unittest.TestCase):
    def test_parses_structured_output_and_noise(self):
        env = Envelope.from_output("Fetching models...\n" + REAL_OK + "\n")
        self.assertEqual(env.status, "SUCCESS")
        self.assertTrue(env.conversation_id.startswith("57c4a29e"))
        self.assertEqual(env.structured_output["first_line"], "alpha")
        self.assertEqual(env.denied_actions, ())
        self.assertEqual(env.usage["input_tokens"], 43021)
        self.assertEqual(env.num_turns, 1)
        self.assertAlmostEqual(env.duration_seconds, 7.276747)
        self.assertEqual(env.noise, ("Fetching models...",))
        self.assertEqual(env.raw["json_schema"], {"type": "object"})
        self.assertTrue(env.ok)

    def test_denied_actions_make_it_not_ok(self):
        env = Envelope.from_output(REAL_DENIED)
        self.assertEqual(env.denied_actions, ("RunCommand",))
        self.assertIsNone(env.structured_output)
        self.assertFalse(env.ok)

    def test_response_json_is_a_fallback(self):
        env = Envelope.from_output('{"conversation_id":"c","status":"SUCCESS","response":"{\\"a\\": 1}\\n","usage":{}}')
        self.assertEqual(env.structured_output, {"a": 1})
        self.assertTrue(env.ok)

    def test_no_json_line(self):
        env = Envelope.from_output("garbage\n\nmore garbage\n")
        self.assertEqual(env.status, "NO_OUTPUT")
        self.assertFalse(env.ok)
        self.assertIsNone(env.conversation_id)
        self.assertEqual(env.noise, ("garbage", "more garbage"))

    def test_last_envelope_line_wins_and_other_json_is_noise(self):
        env = Envelope.from_output('{"foo": 1}\n' + REAL_DENIED + "\n" + REAL_OK + "\n")
        self.assertTrue(env.conversation_id.startswith("57c4a29e"))
        self.assertIn('{"foo": 1}', env.noise)
        self.assertEqual(Envelope.from_output('{"foo": 1}\n').status, "NO_OUTPUT")


class TimeoutTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_timeout("20m"), 1200)
        self.assertEqual(parse_timeout("90s"), 90)
        self.assertEqual(parse_timeout("1h"), 3600)
        self.assertEqual(parse_timeout("15"), 15)
        self.assertEqual(parse_timeout(" 2m "), 120)

    def test_bad_value(self):
        with self.assertRaises(ValueError):
            parse_timeout("soon")


class CommandTests(unittest.TestCase):
    def test_default_command_shape(self):
        cmd = AgyExecutor().command("PROMPT", dir=pathlib.Path("/repo"), model="m", schema=pathlib.Path("/s.json"))
        self.assertEqual(cmd[:13], ["agy", "--dangerously-skip-permissions", "-p", "PROMPT", "--add-dir", "/repo", "--model", "m",
                                    "--output-format", "json", "--json-schema", "/s.json", "--print-timeout"])
        self.assertEqual(cmd[13], policy.AGY_TIMEOUT)
        self.assertEqual(len(cmd), 14)

    def test_optional_flags_and_binary(self):
        cmd = AgyExecutor(binary="/opt/agy").command("P", dir=pathlib.Path("/r"), model="m", schema=pathlib.Path("/s"), conversation="c1", mode="plan", effort="low", timeout="5m")
        self.assertEqual(cmd[0], "/opt/agy")
        self.assertIn("--conversation", cmd)
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "c1")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "5m")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.tmpdir.name)
        self.schema = self.tmp / "s.json"
        self.schema.write_text("{}")

    def tearDown(self):
        self.tmpdir.cleanup()

    def fake(self, body: str) -> pathlib.Path:
        script = self.tmp / "fake-agy"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_run_parses_real_output_and_passes_flags(self):
        args_file = self.tmp / "args.txt"
        script = self.fake(f'echo "Fetching models..." >&2\nprintf \'%s\\n\' "$@" > "{args_file}"\nprintf \'%s\\n\' "$FAKE_AGY_OUTPUT"\n')
        with mock.patch.dict(os.environ, {"FAKE_AGY_OUTPUT": REAL_OK}):
            env = AgyExecutor(binary=str(script)).run("PROMPT TEXT", dir=self.tmp, model="m", schema=self.schema, timeout="1m")
        self.assertTrue(env.ok)
        self.assertTrue(env.conversation_id.startswith("57c4a29e"))
        args = args_file.read_text().splitlines()
        self.assertEqual(args[0], "--dangerously-skip-permissions")
        self.assertIn("PROMPT TEXT", args)
        self.assertIn("--json-schema", args)
        self.assertTrue(any("Fetching models" in line for line in env.noise))

    def test_timeout_yields_timeout_status(self):
        script = self.fake("sleep 5\n")
        env = AgyExecutor(binary=str(script), grace_seconds=0).run("P", dir=self.tmp, model="m", schema=self.schema, timeout="1s")
        self.assertEqual(env.status, "TIMEOUT")
        self.assertFalse(env.ok)

    def test_missing_binary_is_no_output(self):
        env = AgyExecutor(binary=str(self.tmp / "absent")).run("P", dir=self.tmp, model="m", schema=self.schema)
        self.assertEqual(env.status, "NO_OUTPUT")
        self.assertTrue(env.noise)


if __name__ == "__main__":
    unittest.main()
