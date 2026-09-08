from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class Snapshot:
    label: str
    commit: str
    tree: str
    files: Mapping[str, str]
    modes: Mapping[str, str]


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ошибка git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


def _git_bytes(repo: Path, env: dict[str, str], *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ошибка git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def take_snapshot(repo: Path, task_id: str, label: str) -> Snapshot:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "ultron"
    env["GIT_AUTHOR_EMAIL"] = "ultron@local"
    env["GIT_COMMITTER_NAME"] = "ultron"
    env["GIT_COMMITTER_EMAIL"] = "ultron@local"

    with tempfile.TemporaryDirectory() as tmpdir:
        env["GIT_INDEX_FILE"] = str(Path(tmpdir) / "index")

        _git(repo, env, "add", "-A", ".")
        tree = _git(repo, env, "write-tree").strip()

        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "-q", "--verify", "HEAD"],
            env=env,
            capture_output=True,
            text=True
        )

        commit_cmd = ["commit-tree", tree]
        if proc.returncode == 0:
            commit_cmd.extend(["-p", proc.stdout.strip()])
        commit_cmd.extend(["-m", f"ultron {task_id} {label}"])

        commit = _git(repo, env, *commit_cmd).strip()

        ref = f"refs/ultron/{task_id}/{label}"
        _git(repo, env, "update-ref", ref, commit)

        ls_tree_bytes = _git_bytes(repo, env, "ls-tree", "-r", "-z", tree)

        files = {}
        modes = {}
        for entry in ls_tree_bytes.split(b'\0'):
            if not entry:
                continue
            meta, path_bytes = entry.split(b'\t', 1)
            mode_bytes, type_bytes, obj_bytes = meta.split(b' ', 2)
            path = path_bytes.decode('utf-8')
            files[path] = obj_bytes.decode('utf-8')
            modes[path] = mode_bytes.decode('utf-8')

        return Snapshot(label=label, commit=commit, tree=tree, files=files, modes=modes)


def diff_paths(a: Snapshot, b: Snapshot) -> list[str]:
    changed = []
    for p in set(a.files) | set(b.files):
        if "__pycache__/" in p or p.endswith(".pyc"):
            continue
        if a.files.get(p) != b.files.get(p):
            changed.append(p)
    return sorted(changed)


def restore_paths(repo: Path, snap: Snapshot, paths: Sequence[str]) -> list[str]:
    env = os.environ.copy()
    restored = []
    for p in paths:
        target = repo / p
        if p in snap.files:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = _git_bytes(repo, env, "cat-file", "blob", snap.files[p])
            target.write_bytes(content)
            if snap.modes[p] == "100755":
                target.chmod(target.stat().st_mode | stat.S_IXUSR)
        else:
            if target.exists():
                target.unlink()
        restored.append(p)
    return restored


def restore_all(repo: Path, current: Snapshot, target: Snapshot) -> list[str]:
    return restore_paths(repo, target, diff_paths(current, target))


def patch_between(repo: Path, a: Snapshot, b: Snapshot) -> str:
    env = os.environ.copy()
    return _git(repo, env, "diff", "--binary", a.commit, b.commit)


def list_snapshots(repo: Path, task_id: str) -> list[str]:
    env = os.environ.copy()
    prefix = f"refs/ultron/{task_id}/"
    out = _git(repo, env, "for-each-ref", "--format=%(refname)", prefix)
    labels = []
    for line in out.splitlines():
        if line.startswith(prefix):
            labels.append(line[len(prefix):])
    return sorted(labels)


def drop_snapshots(repo: Path, task_id: str) -> None:
    env = os.environ.copy()
    prefix = f"refs/ultron/{task_id}/"
    out = _git(repo, env, "for-each-ref", "--format=%(refname)", prefix)
    for ref in out.splitlines():
        _git(repo, env, "update-ref", "-d", ref)
