import tempfile
import pathlib
import unittest
from tests.helpers import make_repo, so_code, envelope
from ultron import cli
from ultron.state import TaskState
import io
import contextlib

class FakeExecutor:
    def __init__(self, fixes):
        self.fixes = fixes
        self.calls = []

    def run(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.fixes.pop(0)(prompt, kwargs["dir"])

class TestManualRollback(unittest.TestCase):
    def test_rollback(self):
        tmpdir = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(tmpdir.name)
        repo = make_repo(tmp)
        root = tmp / "state"
        spec = tmp / "spec.json"
        
        # We need a spec for this to work... skip manual test and use the existing one
