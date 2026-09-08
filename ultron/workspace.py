from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from ultron import policy
from ultron.check import GateRun
from ultron.state import project_slug


class WorkspaceError(RuntimeError):
    """Raised on workspace operations failure."""


def worktree_root(state_root: Path, project_dir: Path) -> Path:
    return state_root / project_slug(project_dir) / "worktrees"


def worktree_path(state_root: Path, project_dir: Path, task_id: str) -> Path:
    return worktree_root(state_root, project_dir) / task_id


def _run_git(
    repo: Path,
    args: Sequence[str],
    *,
    input_str: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_str,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        err = proc.stderr if proc.stderr.strip() else proc.stdout
        lines = err.splitlines()
        tail = "\n".join(lines[-policy.GATE_TAIL_LINES:]) if lines else err.strip()
        raise WorkspaceError(f"git {' '.join(args)} error ({proc.returncode}):\n{tail}")
    return proc


def create_worktree(repo: Path, path: Path, commit: str) -> Path:
    if path.exists():
        remove_worktree(repo, path)

    _run_git(repo, ["worktree", "prune"])
    _run_git(repo, ["worktree", "add", "--detach", str(path), commit])
    return path


def seed(path: Path, commands: Sequence[str]) -> list[GateRun]:
    runs = []
    for cmd in commands:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        
        log_bytes = proc.stdout
        if len(log_bytes) > policy.GATE_LOG_MAX_BYTES:
            log_bytes = log_bytes[:policy.GATE_LOG_MAX_BYTES]
            
        log = log_bytes.decode("utf-8", errors="replace")
        lines = log.splitlines()
        tail = tuple(lines[-policy.GATE_TAIL_LINES:]) if lines else ()
        
        runs.append(GateRun(
            cmd=cmd,
            exit_code=proc.returncode,
            expected=0,
            tail=tail,
            log=log
        ))
        
        if proc.returncode != 0:
            tail_str = "\n".join(tail)
            raise WorkspaceError(f"seed error: '{cmd}' returned {proc.returncode}\n{tail_str}")
            
    return runs


def remove_worktree(repo: Path, path: Path) -> None:
    _run_git(repo, ["worktree", "remove", "--force", str(path)], check=False)
    _run_git(repo, ["worktree", "prune"])


def integrate(repo: Path, patch: str) -> str:
    if not patch:
        return "empty"

    check_proc = _run_git(repo, ["apply", "--check"], input_str=patch, check=False)
    if check_proc.returncode != 0:
        return "conflict"

    _run_git(repo, ["apply"], input_str=patch, check=True)
    return "applied"

