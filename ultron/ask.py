"""`ultron ask`: a quick read-only question to the executor.

No spec, no snapshots, no gate: one call, a short text answer. Read-only is
requested in the prompt and checked afterwards by comparing `git status`;
a change is reported as a warning, never repaired here.
"""

from __future__ import annotations

import pathlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from ultron import policy
from ultron.executor import Executor
from ultron.pace import wait_turn
from ultron.state import journal_append, project_slug


@dataclass(frozen=True)
class AskResult:
    ok: bool
    status: str
    text: str
    model: str
    seconds: float
    usage: dict
    changed: tuple[str, ...]
    saved: pathlib.Path | None
    noise: tuple[str, ...]


def compose_question(question: str, files: Sequence[pathlib.Path]) -> str:
    parts = [policy.ASK_NOTE, "", "ВОПРОС — " + question.strip()]
    if files:
        parts += ["", "ФАЙЛЫ — прочитай сам:"] + [f"- {f}" for f in files]
    return "\n".join(parts) + "\n"


def _git_status(directory: pathlib.Path) -> set[str] | None:
    """Porcelain status lines, or None when the directory is not a git repository."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return {line for line in proc.stdout.splitlines() if line.strip()}


def _save_answer(root: pathlib.Path, directory: pathlib.Path, text: str) -> pathlib.Path | None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / project_slug(directory) / "asks" / f"{stamp}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return path


def ask(
    question: str,
    *,
    dir: pathlib.Path,
    executor: Executor,
    root: pathlib.Path,
    files: Sequence[pathlib.Path] = (),
    model: str = policy.ASK_MODEL,
    timeout: str = policy.ASK_TIMEOUT,
) -> AskResult:
    before = _git_status(dir)
    wait_turn(root)
    env = executor.run(compose_question(question, files), dir=dir, model=model, schema=None, timeout=timeout)
    after = _git_status(dir)
    changed: tuple[str, ...] = ()
    if before is not None and after is not None:
        changed = tuple(sorted(after ^ before))

    response = env.raw.get("response")
    text = response.strip() if isinstance(response, str) else ""
    ok = env.status == "SUCCESS" and bool(text) and not env.denied_actions
    saved = _save_answer(root, dir, text) if text else None

    record = dict(env.raw)
    record["_meta"] = {"task": "ask", "role": "ask", "model": model, "verdict": "answered" if ok else "failed", "changed": list(changed)}
    journal_append(root, dir, record)

    return AskResult(
        ok=ok, status=env.status or "NO_OUTPUT", text=text, model=model,
        seconds=env.duration_seconds, usage=env.usage, changed=changed, saved=saved, noise=env.noise,
    )


def render(result: AskResult) -> str:
    """Compact text for Claude: capped answer plus one status line."""
    lines: list[str] = []
    if result.text:
        body = result.text
        if len(body) > policy.ASK_MAX_CHARS:
            body = body[: policy.ASK_MAX_CHARS].rstrip() + f"\n… обрезано, полный ответ: {result.saved}"
        lines.append(body)
    else:
        lines.append(f"Ответа нет: {result.status}.")
        lines += [f"  {n}" for n in result.noise[-5:]]
    if result.changed:
        lines.append("ВНИМАНИЕ: исполнитель изменил файлы при запрете: " + "; ".join(result.changed[:10]))
    tokens = result.usage.get("total_tokens", 0)
    lines.append(f"[ask · {result.model} · {round(result.seconds)} с · {tokens} токенов]")
    return "\n".join(lines)
