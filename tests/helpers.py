"""Shared test helpers: toy repo fixture, envelope builders, fake executor."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "toy-project"
GATE_CMD = "python3 tests/test_service.py && python3 tools/check.py"
HELLO_ONLY = 'from app.models import User\n\n\ndef greet(user: User) -> str:\n    return f"hello {user.name}"\n'
HELLO_DOC = 'from app.models import User\n\n\ndef greet(user: User) -> str:\n    """Greets the user."""\n    return f"hello {user.name}"\n'

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=True, env=GIT_ENV, capture_output=True, text=True)
    return proc.stdout


def make_repo(tmp: pathlib.Path, name: str = "toy") -> pathlib.Path:
    """Copy the toy project into tmp and commit it so scope checks have a baseline."""
    dst = tmp / name
    shutil.copytree(FIXTURE, dst)
    git(dst, "init", "-q")
    git(dst, "add", "-A")
    git(dst, "commit", "-q", "-m", "init")
    return dst


def write_spec(tmp: pathlib.Path, repo: pathlib.Path, *, role: str = "code", brief: str = "Роль: code\nЗАДАЧА — поменять приветствие.\n", **fields) -> pathlib.Path:
    task_id = fields.get("id", "T-1")
    brief_path = tmp / f"brief-{task_id}.md"
    brief_path.write_text(brief, encoding="utf-8")
    data = {"id": task_id, "role": role, "dir": str(repo), "brief": brief_path.name}
    data.update(fields)
    spec_path = tmp / f"spec-{task_id}.json"
    spec_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return spec_path


def envelope(so: dict | None = None, *, status: str = "SUCCESS", conversation: str | None = "conv-1", denied=(), usage: dict | None = None, turns: int = 1, seconds: float = 1.5, response: str | None = None):
    from ultron.executor import Envelope

    usage = usage or {"input_tokens": 100, "output_tokens": 10, "thinking_tokens": 5, "cache_read_tokens": 20, "total_tokens": 110}
    raw = {"status": status, "conversation_id": conversation, "usage": usage, "num_turns": turns, "duration_seconds": seconds}
    if so is not None:
        raw["structured_output"] = so
    if response is not None:
        raw["response"] = response
    if denied:
        raw["denied_actions"] = [{"action": "command", "display_name": d} for d in denied]
    return Envelope(status=status, conversation_id=conversation, structured_output=so, denied_actions=tuple(denied), usage=usage, num_turns=turns, duration_seconds=seconds, raw=raw, noise=())


def so_common(status: str = "done", summary: str = "Сделано.", evidence=None, open_questions=None, next_action: str = "ничего") -> dict:
    return {
        "status": status,
        "summary": summary,
        "evidence": list(evidence or [{"id": "e1", "where": "app/service.py:5", "excerpt": 'return f"hello {user.name}"'}]),
        "open_questions": list(open_questions or []),
        "next_action": next_action,
    }


def so_code(changed=(("app/service.py", "modified", "приветствие"),), commands=(), not_done=(), patch: str = "", **common) -> dict:
    out = so_common(**common)
    out.update({
        "changed_files": [{"path": p, "action": a, "what": w} for p, a, w in changed],
        "commands": [{"cmd": c, "exit_code": e, "summary": s} for c, e, s in commands],
        "not_done": list(not_done),
        "patch": patch,
    })
    return out


def so_explore(map_entries=(("app/service.py", "greet", ()),), facts=(), **common) -> dict:
    out = so_common(**common)
    out.update({
        "map": [{"path": p, "purpose": u, "depends_on": list(d)} for p, u, d in map_entries],
        "facts": [{"claim": c, "verdict": v, "where": w} for c, v, w in facts],
    })
    return out


def so_verify(gate=(GATE_CMD, 1, 1, 1, 0), failures=(), warnings=(), **common) -> dict:
    out = so_common(**common)
    cmd, exit_code, passed, failed, skipped = gate
    out.update({
        "gate": {"command": cmd, "exit_code": exit_code, "passed": passed, "failed": failed, "skipped": skipped},
        "failures": [{"test": t, "where": w, "reason": r, "suspected_cause": c} for t, w, r, c in failures],
        "warnings": list(warnings),
    })
    return out


def so_review(verdicts=(), risks=(), recommendation=("оставить", "так надёжнее"), **common) -> dict:
    out = so_common(**common)
    out.update({
        "verdicts": [{"claim": c, "verdict": v, "where": w, "note": n} for c, v, w, n in verdicts],
        "risks": [{"risk": r, "where": w, "mitigation": m} for r, w, m in risks],
        "recommendation": {"choice": recommendation[0], "reasoning": recommendation[1]},
    })
    return out


class FakeExecutor:
    """Scripted executor: each step is an Envelope or a callable(prompt, dir) -> Envelope.

    Callables may mutate the repository to imitate the executor's edits.
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.raw_schema: list = []

    def run(self, prompt, *, dir, model, schema, conversation=None, mode=None, effort=None, timeout="20m"):
        self.raw_schema.append(schema)
        self.calls.append({"prompt": prompt, "dir": pathlib.Path(dir), "model": model, "schema": pathlib.Path(schema) if schema is not None else None, "conversation": conversation, "mode": mode, "effort": effort, "timeout": timeout})
        if not self.steps:
            raise AssertionError("FakeExecutor: no steps left")
        step = self.steps.pop(0)
        return step(prompt, pathlib.Path(dir)) if callable(step) else step


def result_dict(**over) -> dict:
    """A minimal valid TaskResult for view/cli tests."""
    base = {
        "id": "T-1", "role": "code", "status": "accepted", "executor_status": "done", "attempts": 1, "tier": "gemini-3.1-pro-high",
        "summary": "Приветствие заменено.", "changed_files": ["app/service.py"], "claimed_files": ["app/service.py"],
        "checks": {"contract": "ok", "scope": "ok", "gate": {"exit": 0, "expected": 0, "attempt": 1, "log": "gate.log"}, "violations": [], "restored": []},
        "failure": {"domain": "none", "kind": ""},
        "role_fields": {"changed_files": [{"path": "app/service.py", "action": "modified", "what": "hi → hello"}], "commands": [], "not_done": [], "patch": ""},
        "evidence": [{"id": "e1", "where": "app/service.py:5"}], "open_questions": [], "next_action": "ничего",
        "usage": {"executor_in": 100, "cache_read": 20, "executor_out": 10, "thinking": 5, "seconds": 1.5},
        "refs": {"state": "/tmp/state/T-1", "conversation": "conv-1", "patch": None, "snapshots": {"baseline": "0" * 40, "attempts": ["1" * 40]}},
    }
    base.update(over)
    return base
