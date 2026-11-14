"""CLI interface for Ultron."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Sequence

from ultron.executor import AgyExecutor, Executor
from ultron.run import continue_task, run_task
from ultron.spec import load_spec, parse_spec
from ultron.state import TaskState, list_tasks, state_root
from ultron.view import compact, evidence_text


def main(argv: Sequence[str] | None = None, executor: Executor | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ultron")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("spec", type=pathlib.Path)
    run_parser.add_argument("--state", type=pathlib.Path)
    run_parser.add_argument("--dry-run", action="store_true")

    # show command
    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("task")
    show_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    show_parser.add_argument("--state", type=pathlib.Path)
    group = show_parser.add_mutually_exclusive_group()
    group.add_argument("--evidence")
    group.add_argument("--gate", action="store_true")
    group.add_argument("--diff", action="store_true")
    group.add_argument("--raw", action="store_true")
    group.add_argument("--brief", action="store_true")

    # retry command
    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("task")
    retry_parser.add_argument("--message", type=pathlib.Path, required=True)
    retry_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    retry_parser.add_argument("--state", type=pathlib.Path)

    # list command
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    list_parser.add_argument("--state", type=pathlib.Path)

    # rollback command
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("task")
    rollback_parser.add_argument("--to", required=True)
    rollback_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    rollback_parser.add_argument("--state", type=pathlib.Path)

    # integrate command
    integrate_parser = subparsers.add_parser("integrate")
    integrate_parser.add_argument("task")
    integrate_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    integrate_parser.add_argument("--state", type=pathlib.Path)

    # clean command
    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("task")
    clean_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    clean_parser.add_argument("--state", type=pathlib.Path)

    # ask command: quick read-only question, no spec
    ask_parser = subparsers.add_parser("ask")
    ask_parser.add_argument("question")
    ask_parser.add_argument("--file", action="append", default=[], type=pathlib.Path)
    ask_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    ask_parser.add_argument("--model")
    ask_parser.add_argument("--force", action="store_true")
    ask_parser.add_argument("--state", type=pathlib.Path)

    # note command: record who a piece of work went to and why
    note_parser = subparsers.add_parser("note")
    note_parser.add_argument("what")
    note_parser.add_argument("--to", required=True)
    note_parser.add_argument("--why", default="")
    note_parser.add_argument("--dir", type=pathlib.Path, default=pathlib.Path.cwd())
    note_parser.add_argument("--state", type=pathlib.Path)

    # budget command: learned limits and the current mode per model family
    budget_parser = subparsers.add_parser("budget")
    budget_parser.add_argument("--state", type=pathlib.Path)

    # usage command: calls and tokens over the subscription windows
    usage_parser = subparsers.add_parser("usage")
    usage_parser.add_argument("--state", type=pathlib.Path)

    args = parser.parse_args(argv)

    root = args.state.absolute() if args.state else state_root()
    exec_inst = executor or AgyExecutor()

    if args.command == "run":
        spec = load_spec(args.spec.absolute())
        state = TaskState(root, spec.dir, spec.id)
        if args.dry_run:
            brief_text = spec.brief.read_text(encoding="utf-8")
            if hasattr(exec_inst, "command"):
                cmd = exec_inst.command(
                    f"<бриф {len(brief_text)} символов>",
                    dir=spec.dir,
                    model=spec.executor.model or spec.budget.tiers[0],
                    schema=pathlib.Path(__file__).parent / "schemas" / "roles" / f"{spec.role}.json",
                    mode=spec.executor.mode,
                    effort=spec.executor.effort,
                )
                print(" ".join(cmd))
            return 0
            
        from ultron import policy as _policy

        brief_chars = len(spec.brief.read_text(encoding="utf-8"))
        if brief_chars > _policy.BRIEF_WARN_CHARS:
            print(f"warning: бриф {brief_chars} символов при пороге {_policy.BRIEF_WARN_CHARS} — передача может стоить дороже, чем сделать самому")
        res = run_task(spec, executor=exec_inst, state=state)
        
        print(compact(res))
        return 0 if res.get("status") == "accepted" else 2

    elif args.command == "ask":
        from ultron import policy
        from ultron.ask import ask, render

        directory = args.dir.absolute()
        if not directory.is_dir():
            print(f"Каталог {directory} не найден.", file=sys.stderr)
            return 1
        if args.file and not args.force:
            total = 0
            for f in args.file:
                try:
                    total += len((f if f.is_absolute() else directory / f).read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    total = policy.ASK_MIN_LINES
                    break
            if total < policy.ASK_MIN_LINES:
                print(f"Не вызвано: в названных файлах {total} строк при пороге {policy.ASK_MIN_LINES} — прочитать самому дешевле. --force, чтобы всё равно спросить.")
                return 3
        result = ask(args.question, dir=directory, executor=exec_inst, root=root, files=args.file, model=args.model or policy.ASK_MODEL)
        print(render(result))
        return 0 if result.ok else 2

    elif args.command == "note":
        from ultron import policy
        from ultron.state import journal_append

        if args.to not in policy.NOTE_TARGETS:
            print(f"--to: одно из {', '.join(policy.NOTE_TARGETS)}", file=sys.stderr)
            return 1
        journal_append(root, args.dir.absolute(), {"_meta": {"role": "decision", "to": args.to, "what": args.what, "why": args.why}})
        return 0

    elif args.command == "budget":
        from ultron import limits, policy

        models = {policy.ASK_MODEL, *(tier for role in policy.ROLES.values() for tier in role.tiers if tier.startswith("gemini-"))}
        seen: dict = {}
        for model in sorted(models):
            b = limits.budget(root, model)
            seen.setdefault(b.family, b)
        print(limits.render(list(seen.values())))
        return 0

    elif args.command == "usage":
        from ultron.usage import render as render_usage, summarize

        print(render_usage(summarize(root)))
        return 0

    elif args.command == "show":
        state = TaskState(root, args.dir.absolute(), args.task)
        res = state.load_result()
        if not res:
            print(f"Задача {args.task} не найдена.", file=sys.stderr)
            return 1

        if args.evidence:
            attempts = state.load_attempts()
            text = evidence_text(attempts, args.evidence)
            if text:
                print(text)
        elif args.gate:
            if state.gate_log_path.exists():
                print(state.gate_log_path.read_text(encoding="utf-8"))
        elif args.diff:
            cf = res.get("changed_files", [])
            if cf:
                proc_stat = subprocess.run(["git", "diff", "--stat", "--"] + cf, cwd=args.dir.absolute(), capture_output=True, text=True)
                print(proc_stat.stdout, end="")
                proc_diff = subprocess.run(["git", "diff", "--"] + cf, cwd=args.dir.absolute(), capture_output=True, text=True)
                print(proc_diff.stdout, end="")
        elif args.raw:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.brief:
            if state.brief_path.exists():
                print(state.brief_path.read_text(encoding="utf-8"), end="")
        else:
            print(compact(res))
        return 0

    elif args.command == "retry":
        state = TaskState(root, args.dir.absolute(), args.task)
        spec_data = state.load_spec()
        if not spec_data:
            print(f"Задача {args.task} не найдена.", file=sys.stderr)
            return 1
        spec = parse_spec(spec_data, args.dir.absolute())
        msg = args.message.read_text(encoding="utf-8")
        res = continue_task(spec, msg, executor=exec_inst, state=state)
        
        print(compact(res))
        return 0 if res.get("status") == "accepted" else 2

    elif args.command == "list":
        tasks = list_tasks(root, args.dir.absolute())
        if tasks:
            print("\n".join(tasks))
        return 0

    elif args.command == "rollback":
        from ultron.snapshot import restore_all, take_snapshot, list_snapshots, Snapshot
        import os
        
        state = TaskState(root, args.dir.absolute(), args.task)
        res = state.load_result()
        if not res:
            print(f"Задача {args.task} не найдена.", file=sys.stderr)
            return 1
            
        target_label = args.to
        snapshots = list_snapshots(args.dir.absolute(), args.task)
        if target_label not in snapshots:
            print(f"Снимок {target_label} не найден.", file=sys.stderr)
            return 1
            
        current = take_snapshot(args.dir.absolute(), args.task, "current")
        
        env = os.environ.copy()
        repo = args.dir.absolute()
        target_ref = f"refs/ultron/{args.task}/{target_label}"
        target_commit = subprocess.run(["git", "-C", str(repo), "rev-parse", target_ref], env=env, capture_output=True, text=True).stdout.strip()
        target_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{target_commit}^{{tree}}"], env=env, capture_output=True, text=True).stdout.strip()
        
        ls_tree_bytes = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", target_tree], env=env, capture_output=True).stdout
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
            
        target = Snapshot(label=target_label, commit=target_commit, tree=target_tree, files=files, modes=modes)
        restored = restore_all(repo, current, target)
        if restored:
            print("\n".join(restored))
        return 0

    elif args.command == "integrate":
        from ultron.workspace import integrate as ws_integrate
        
        state = TaskState(root, args.dir.absolute(), args.task)
        res = state.load_result()
        if not res:
            print(f"Задача {args.task} не найдена.", file=sys.stderr)
            return 1
            
        patch_path = state.patch_path
        patch = ""
        if patch_path.exists():
            patch = patch_path.read_text(encoding="utf-8")
            
        outcome = ws_integrate(args.dir.absolute(), patch)
        print(outcome)
        
        if outcome == "applied":
            res["refs"]["integration"] = "applied"
            state.save_result(res)
            return 0
        return 1

    elif args.command == "clean":
        from ultron.workspace import remove_worktree, worktree_path
        
        work = worktree_path(root, args.dir.absolute(), args.task)
        remove_worktree(args.dir.absolute(), work)
        return 0

    return 1
