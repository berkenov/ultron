"""Task state and journal management.

Handles reading and writing task specifications, attempts, and results
to the persistent state directory, typically located outside the repository.
"""

from __future__ import annotations

import json
import os
import pathlib

from ultron import policy


def state_root() -> pathlib.Path:
    """Return the state root directory, from environment or default."""
    env_root = os.environ.get(policy.STATE_ROOT_ENV)
    if env_root is not None:
        return pathlib.Path(env_root)
    return pathlib.Path(policy.DEFAULT_STATE_ROOT).expanduser()


def project_slug(project_dir: pathlib.Path) -> str:
    """Return a flat string representing the absolute path of a project."""
    return str(project_dir).replace("\\", "/").replace("/", "-")


class TaskState:
    """Manages files related to a single task inside the state directory."""

    def __init__(self, root: pathlib.Path, project_dir: pathlib.Path, task_id: str):
        self.root = root
        self.project_dir = project_dir
        self.task_id = task_id

        self.dir = root / project_slug(project_dir) / "tasks" / task_id
        self.spec_path = self.dir / "spec.json"
        self.brief_path = self.dir / "brief.md"
        self.attempts_path = self.dir / "attempts.jsonl"
        self.gate_log_path = self.dir / "gate.log"
        self.patch_path = self.dir / "patch.diff"
        self.result_path = self.dir / "result.json"

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_spec(self, data: dict) -> None:
        """Save the task specification to spec.json."""
        self._ensure_dir()
        with self.spec_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def load_spec(self) -> dict | None:
        """Load the task specification from spec.json."""
        if not self.spec_path.exists():
            return None
        with self.spec_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save_brief(self, text: str) -> None:
        """Save the brief text to brief.md."""
        self._ensure_dir()
        with self.brief_path.open("w", encoding="utf-8") as f:
            f.write(text)

    def append_attempt(self, record: dict) -> None:
        """Append an attempt record to attempts.jsonl."""
        self._ensure_dir()
        with self.attempts_path.open("a", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.write("\n")

    def load_attempts(self) -> list[dict]:
        """Load all attempt records from attempts.jsonl."""
        if not self.attempts_path.exists():
            return []
        attempts = []
        with self.attempts_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    attempts.append(json.loads(line))
        return attempts

    def save_gate_log(self, text: str) -> None:
        """Save the gate execution log to gate.log."""
        self._ensure_dir()
        with self.gate_log_path.open("w", encoding="utf-8") as f:
            f.write(text)

    def save_patch(self, text: str) -> pathlib.Path:
        """Save a unified diff patch to patch.diff and return its path."""
        self._ensure_dir()
        with self.patch_path.open("w", encoding="utf-8") as f:
            f.write(text)
        return self.patch_path

    def save_result(self, result: dict) -> None:
        """Save the task result to result.json."""
        self._ensure_dir()
        with self.result_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)

    def load_result(self) -> dict | None:
        """Load the task result from result.json."""
        if not self.result_path.exists():
            return None
        with self.result_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def attempt_patch_path(self, n: int) -> pathlib.Path:
        return self.dir / f"attempt-{n}.diff"

    def save_attempt_patch(self, n: int, text: str) -> pathlib.Path:
        """Save an attempt patch and return its path."""
        self._ensure_dir()
        path = self.attempt_patch_path(n)
        with path.open("w", encoding="utf-8") as f:
            f.write(text)
        return path

    def journal_append(self, record: dict) -> None:
        """Append a record to the project's journal.jsonl."""
        journal_path = self.root / project_slug(self.project_dir) / "journal.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.write("\n")

    def reset(self) -> None:
        """Remove attempts, gate log, patch, result, and attempt patches, keeping the task dir."""
        for path in (self.attempts_path, self.gate_log_path, self.patch_path, self.result_path):
            if path.exists():
                path.unlink()
        if self.dir.exists():
            for p in self.dir.glob("attempt-*.diff"):
                if p.is_file():
                    p.unlink()


def list_tasks(root: pathlib.Path, project_dir: pathlib.Path) -> list[str]:
    """List all task IDs in the project state, sorted."""
    tasks_dir = root / project_slug(project_dir) / "tasks"
    if not tasks_dir.exists():
        return []
    tasks = []
    for p in tasks_dir.iterdir():
        if p.is_dir():
            tasks.append(p.name)
    return sorted(tasks)
