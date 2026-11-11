from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ultron import policy
from ultron.check import CheckReport, check_all, load_role_schema, check_scope, check_diff_size, DIFF_LIMIT_PREFIX
from ultron.compose import AttemptSummary, compose_prompt, escalation_prompt, retry_message, report_only_message
from ultron.executor import Envelope, Executor
from ultron.spec import Spec, SpecError
from ultron.state import TaskState
from ultron.snapshot import take_snapshot, diff_paths, restore_paths, restore_all, patch_between, drop_snapshots
from ultron.workspace import WorkspaceError, worktree_path, create_worktree, seed, remove_worktree, integrate


@dataclass
class Attempt:
    number: int
    tier_index: int
    model: str
    conversation: str | None
    prompt: str
    envelope: Envelope
    report: CheckReport
    changed: list[str]
    restored: list[str]
    snapshot_label: str
    snapshot_commit: str
    snapshot_tree: str


def build_result(
    spec: Spec,
    attempts: Sequence[Attempt],
    status: str,
    changed_files: Sequence[str],
    state: TaskState
) -> dict:
    if not attempts:
        raise ValueError("No attempts to build result from")
    
    last = attempts[-1]
    
    executor_status = None
    if last.envelope.structured_output:
        executor_status = last.envelope.structured_output.get("status")
        
    claimed_files = []
    if last.envelope.structured_output:
        for item in last.envelope.structured_output.get("changed_files", []):
            if isinstance(item, dict) and "path" in item:
                claimed_files.append(item["path"])
                
    restored_all = []
    for a in attempts:
        for r in a.restored:
            if r not in restored_all:
                restored_all.append(r)

    scope_status = "ok"
    if last.report.scope:
        scope_status = "violated"
    elif restored_all:
        scope_status = "restored"

    checks = {
        "contract": "ok" if not last.report.contract else "violated",
        "scope": scope_status,
        "gate": None,
        "violations": list(last.report.violations),
        "restored": restored_all,
    }
    
    # Overwrite if contract is missing (from the special accepted case)
    if status == "accepted" and last.report.contract:
        checks["contract"] = "missing"
    
    if last.report.gate:
        last_gate = last.report.gate[-1]
        checks["gate"] = {
            "exit": last_gate.exit_code,
            "expected": last_gate.expected,
            "attempt": last.number,
            "log": "gate.log",
        }
            
    role_fields = {}
    evidence = []
    open_questions = []
    next_action = "ничего"
    summary = ""
    
    if last.envelope.structured_output:
        so = last.envelope.structured_output
        summary = so.get("summary", "")
        for e in so.get("evidence", []):
            if isinstance(e, dict) and "id" in e and "where" in e:
                evidence.append({"id": e["id"], "where": e["where"]})
        open_questions = so.get("open_questions", [])
        next_action = so.get("next_action", "ничего")
        for k, v in so.items():
            if k not in policy.COMMON_FIELDS:
                role_fields[k] = v

    if status == "accepted" and checks["contract"] == "missing":
        summary = "Отчёт исполнителя не получен; код принят по гейту и объёму."
        executor_status = None

    usage = {
        "executor_in": sum(a.envelope.usage.get("input_tokens", 0) for a in attempts),
        "cache_read": sum(a.envelope.usage.get("cache_read_tokens", 0) for a in attempts),
        "executor_out": sum(a.envelope.usage.get("output_tokens", 0) for a in attempts),
        "thinking": sum(a.envelope.usage.get("thinking_tokens", 0) for a in attempts),
        "seconds": sum(a.envelope.duration_seconds for a in attempts),
    }
    
    patch_path = None
    if last.envelope.structured_output and last.envelope.structured_output.get("patch"):
        patch_path = str(state.patch_path)

    domain = "none"
    kind = ""
    
    if status == "blocked":
        domain = "decision"
        kind = "blocked"
    elif status == "failed":
        domain = "infra"
        if not last.envelope.structured_output and last.envelope.status in ("NO_OUTPUT", "TIMEOUT"):
            kind = last.envelope.status.lower()
        else:
            # check if budget tokens or minutes
            if any("budget" in v and "limit exceeded" in v for v in last.report.contract):
                kind = "budget_tokens" # approximate
            else:
                kind = "budget_tokens"
    elif status == "rejected":
        domain = "execution"
        if last.report.contract:
            kind = "contract"
        elif last.report.scope:
            kind = "scope"
        else:
            kind = "gate"
            
    # For semantic kind in continue_task, it should be overridden below, but this is the default

    return {
        "id": spec.id,
        "role": spec.role,
        "status": status,
        "executor_status": executor_status,
        "attempts": last.number,
        "tier": last.model,
        "summary": summary,
        "changed_files": list(changed_files),
        "claimed_files": claimed_files,
        "checks": checks,
        "role_fields": role_fields,
        "evidence": evidence,
        "open_questions": open_questions,
        "next_action": next_action,
        "usage": usage,
        "refs": {
            "state": str(state.dir),
            "conversation": last.envelope.conversation_id,
            "patch": patch_path,
            "snapshots": {
                "baseline": state.load_result().get("refs", {}).get("snapshots", {}).get("baseline", "") if state.load_result() else "",
                "attempts": [a.snapshot_commit for a in attempts]
            }
        },
        "failure": {
            "domain": domain,
            "kind": kind
        }
    }


def _check_budget(spec: Spec, attempts: list[Attempt], clock: Callable[[], float], start_time: float) -> str | None:
    total_tokens = sum(a.envelope.usage.get("total_tokens", 0) for a in attempts)
    if total_tokens > spec.budget.executor_tokens:
        return "budget_tokens"
    elapsed_minutes = (clock() - start_time) / 60.0
    if elapsed_minutes > spec.budget.minutes:
        return "budget_minutes"
    return None


def _run_loop(
    spec: Spec,
    executor: Executor,
    state: TaskState,
    schema_dir: Path | None,
    clock: Callable[[], float],
    initial_prompt: str,
    start_tier: int,
    start_attempt: int,
    attempts: list[Attempt],
    start_conversation: str | None,
    brief_text: str | None,
    inputs: Mapping[str, dict] | None,
    semantic_kind: str | None = None
) -> dict:
    schema_path = (schema_dir or (Path(__file__).parent / "schemas" / "roles")) / f"{spec.role}.json"
    schema = load_role_schema(spec.role, schema_dir)
    
    tier_idx = start_tier
    attempt_num = start_attempt
    conversation = start_conversation
    prompt = initial_prompt
    start_time = clock()
    
    status = "failed"
    
    repo_dir = spec.dir
    work_dir = spec.dir
    
    if spec.workspace.mode == "worktree":
        work_path = worktree_path(state.root, repo_dir, spec.id)
        baseline = take_snapshot(repo_dir, spec.id, "baseline")
        if start_attempt > 1:
            if not work_path.exists():
                raise WorkspaceError("worktree is missing for continue_task")
            work_dir = work_path
        else:
            work_dir = create_worktree(repo_dir, work_path, baseline.commit)
            try:
                seed(work_dir, spec.workspace.seed)
            except WorkspaceError as e:
                env = Envelope(status="NO_OUTPUT", conversation_id=None, structured_output=None, denied_actions=(), usage={}, num_turns=0, duration_seconds=0.0, raw={}, noise=())
                report = CheckReport(contract=(str(e),), scope=(), gate=(), anchors=(), warnings=())
                attempts.append(Attempt(1, 0, "", None, "", env, report, [], [], "", "", ""))
                res = build_result(spec, attempts, "failed", [], state)
                res["failure"] = {"domain": "infra", "kind": "seed"}
                res["refs"]["worktree"] = str(work_path)
                res["refs"]["integration"] = None
                state.save_result(res)
                return res
    else:
        baseline = take_snapshot(repo_dir, spec.id, "baseline")
    
    report_retries = 0

    while True:
        model = spec.budget.tiers[tier_idx]
        
        env = executor.run(
            prompt,
            dir=work_dir,
            model=model,
            schema=schema_path,
            conversation=conversation,
            mode=spec.executor.mode,
            effort=spec.executor.effort,
        )
        
        snap = take_snapshot(work_dir, spec.id, f"attempt-{attempt_num}")
        changed = diff_paths(baseline, snap)
        
        violations = check_scope(changed, spec)
        size_violations = []
        if spec.scope.max_diff_lines is not None:
            patch = patch_between(work_dir, baseline, snap)
            size_violations = check_diff_size(patch, spec.scope.max_diff_lines)
            if size_violations:
                violations.extend(size_violations)

        restored = []
        if violations:
            paths_to_restore = []
            for v in violations:
                if v.startswith("роль ") and " изменено: " in v:
                    paths_to_restore.extend(v.split(" изменено: ")[1].split(", "))
                elif v.startswith("вне разрешённого объёма: "):
                    paths_to_restore.append(v.split("вне разрешённого объёма: ")[1])
                elif v.startswith(DIFF_LIMIT_PREFIX):
                    paths_to_restore.extend(changed)
            
            restored = restore_paths(work_dir, baseline, list(set(paths_to_restore)))
            snap = take_snapshot(work_dir, spec.id, f"attempt-{attempt_num}")
            changed = diff_paths(baseline, snap)
            
        report = check_all(env, spec, schema, changed, cwd=work_dir)
        if size_violations:
            report = CheckReport(
                contract=report.contract,
                scope=tuple(list(report.scope) + size_violations),
                gate=report.gate,
                anchors=report.anchors,
                warnings=report.warnings
            )
        
        attempt = Attempt(
            number=attempt_num,
            tier_index=tier_idx,
            model=model,
            conversation=env.conversation_id,
            prompt=prompt,
            envelope=env,
            report=report,
            changed=changed,
            restored=restored,
            snapshot_label=snap.label,
            snapshot_commit=snap.commit,
            snapshot_tree=snap.tree,
        )
        attempts.append(attempt)
        
        record = dict(env.raw)
        record["_meta"] = {
            "number": attempt.number,
            "tier": attempt.tier_index,
            "model": attempt.model,
            "violations": list(report.violations),
            "warnings": list(report.warnings),
            "changed": changed,
            "restored": restored,
            "snapshot": {
                "label": snap.label,
                "commit": snap.commit,
                "tree": snap.tree
            }
        }
        if semantic_kind:
            record["_meta"]["kind"] = semantic_kind
            
        state.append_attempt(record)
        
        if report.gate:
            last_gate = report.gate[-1]
            if last_gate.log:
                state.save_gate_log(last_gate.log)
                
        if env.structured_output and env.structured_output.get("patch"):
            state.save_patch(env.structured_output["patch"])
            
        budget_failure = _check_budget(spec, attempts, clock, start_time)
        
        executor_status = env.structured_output.get("status") if env.structured_output else None
        
        
        if executor_status == "blocked":
            status = "blocked"
            verdict = "blocked"
        elif not env.structured_output and env.status in ("NO_OUTPUT", "TIMEOUT"):
            status = "failed"
            verdict = "failed"
        elif budget_failure:
            status = "failed"
            verdict = "failed"
            viol = list(report.contract)
            viol.append(f"budget: {budget_failure} limit exceeded")
            attempt.report = CheckReport(
                contract=tuple(viol),
                scope=report.scope,
                gate=report.gate,
                anchors=report.anchors,
                warnings=report.warnings
            )
        elif report.ok:
            status = "accepted"
            verdict = "accepted"
        else:
            gate_ok = all(g.ok for g in report.gate) if report.gate else True
            scope_ok = not report.scope
            
            if gate_ok and scope_ok and report.contract:
                if report_retries == 0:
                    prompt = report_only_message(spec)
                    conversation = env.conversation_id
                    attempt_num += 1
                    report_retries = 1
                    verdict = "retry"
                else:
                    status = "accepted"
                    verdict = "accepted"
            else:
                attempts_this_tier = len([a for a in attempts if a.tier_index == tier_idx and "report_only" not in getattr(a, "prompt", "")])
                if report_retries > 0:
                    attempts_this_tier -= 1
                    
                if attempts_this_tier < spec.budget.attempts_per_tier:
                    prompt = retry_message(report, spec, restored)
                    conversation = env.conversation_id
                    attempt_num += 1
                    verdict = "retry"
                else:
                    if tier_idx + 1 < len(spec.budget.tiers):
                        tier_idx += 1
                        attempt_num += 1
                        conversation = None
                        
                        restore_all(work_dir, snap, baseline)
                        patch = patch_between(work_dir, baseline, snap)
                        
                        patch_file = state.save_attempt_patch(attempt.number, patch)
                        
                        prev_summaries = []
                        for a in attempts:
                            so = a.envelope.structured_output or {}
                            summ = so.get("summary", "")
                            gate_tail = ()
                            if a.report.gate and not a.report.gate[-1].ok:
                                gate_tail = a.report.gate[-1].tail
                            prev_summaries.append(AttemptSummary(
                                number=a.number,
                                model=a.model,
                                summary=summ,
                                violations=a.report.violations,
                                gate_tail=gate_tail,
                            ))
                        prompt = escalation_prompt(spec, brief_text or "", inputs or {}, prev_summaries, patch_file)
                        verdict = "retry"
                    else:
                        status = "rejected"
                        verdict = "rejected"
                    
        j_record = dict(record)
        j_record["_meta"] = dict(record["_meta"])
        j_record["_meta"]["task"] = spec.id
        j_record["_meta"]["role"] = spec.role
        j_record["_meta"]["verdict"] = verdict
        state.journal_append(j_record)
        
        if verdict in ("accepted", "blocked", "failed", "rejected"):
            break
                
    final_snap = take_snapshot(work_dir, spec.id, "final")
    final_changed = diff_paths(baseline, final_snap)
    
    res = build_result(spec, attempts, status, final_changed, state)
    
    if semantic_kind and status == "rejected":
        res["failure"] = {"domain": "semantic", "kind": semantic_kind}
        
    res["refs"]["snapshots"]["baseline"] = baseline.commit
    
    worktree_ref = None
    integration_ref = None
    
    if spec.workspace.mode == "worktree":
        work_path = worktree_path(state.root, repo_dir, spec.id)
        if status == "accepted":
            patch = patch_between(work_dir, baseline, final_snap)
            integration_ref = integrate(repo_dir, patch)
            if integration_ref == "applied":
                remove_worktree(repo_dir, work_path)
            elif integration_ref == "conflict":
                state.save_patch(patch)
                worktree_ref = str(work_path)
                res["refs"]["patch"] = str(state.patch_path)
            elif integration_ref == "empty":
                remove_worktree(repo_dir, work_path)
        else:
            worktree_ref = str(work_path)
            
    res["refs"]["worktree"] = worktree_ref
    if spec.workspace.mode == "worktree":
        res["refs"]["integration"] = integration_ref
    else:
        res["refs"]["integration"] = None
        
    state.save_result(res)
    return res


def run_task(
    spec: Spec,
    *,
    executor: Executor,
    state: TaskState,
    inputs: Mapping[str, dict] | None = None,
    schema_dir: Path | None = None,
    clock: Callable[[], float] = time.monotonic
) -> dict:
    if inputs is None:
        inputs_dict = {}
        for ref in spec.inputs:
            ref_state = TaskState(state.root, spec.dir, ref.task)
            res = ref_state.load_result()
            if res is None:
                raise SpecError(f"inputs: нет результата задачи {ref.task}")
            inputs_dict[ref.task] = res
        inputs = inputs_dict

    state.reset()
    drop_snapshots(spec.dir, spec.id)

    spec_data = {
        "id": spec.id,
        "role": spec.role,
        "dir": str(spec.dir),
        "brief": str(spec.brief),
        "goal": spec.goal,
        "scope": {"write": list(spec.scope.write), "forbid": list(spec.scope.forbid)},
        "inputs": [{"task": inp.task, "fields": list(inp.fields)} for inp in spec.inputs],
        "acceptance": [{"cmd": acc.cmd, "expect_exit": acc.expect_exit} for acc in spec.acceptance],
        "budget": {
            "attempts_per_tier": spec.budget.attempts_per_tier,
            "tiers": list(spec.budget.tiers),
            "executor_tokens": spec.budget.executor_tokens,
            "minutes": spec.budget.minutes,
        },
        "executor": {
            "model": spec.executor.model,
            "effort": spec.executor.effort,
            "mode": spec.executor.mode,
        },
        "workspace": {
            "mode": spec.workspace.mode,
            "seed": list(spec.workspace.seed)
        }
    }
    state.save_spec(spec_data)

    brief_text = spec.brief.read_text(encoding="utf-8")
    first_prompt = compose_prompt(spec, brief_text, inputs)
    state.save_brief(first_prompt)

    return _run_loop(
        spec=spec,
        executor=executor,
        state=state,
        schema_dir=schema_dir,
        clock=clock,
        initial_prompt=first_prompt,
        start_tier=0,
        start_attempt=1,
        attempts=[],
        start_conversation=None,
        brief_text=brief_text,
        inputs=inputs,
    )


def continue_task(
    spec: Spec,
    message: str,
    *,
    executor: Executor,
    state: TaskState,
    schema_dir: Path | None = None,
    clock: Callable[[], float] = time.monotonic
) -> dict:
    attempts_data = state.load_attempts()
    attempts = []
    
    for data in attempts_data:
        meta = data.get("_meta", {})
        
        raw = data
        env = Envelope(
            status=raw.get("status"),
            conversation_id=raw.get("conversation_id"),
            structured_output=raw.get("structured_output"),
            denied_actions=tuple(d.get("display_name", "") for d in raw.get("denied_actions", [])),
            usage=raw.get("usage", {}),
            num_turns=raw.get("num_turns"),
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
            raw=raw,
            noise=(),
        )
        
        report = CheckReport(contract=(), scope=(), gate=(), anchors=(), warnings=())
        snap_meta = meta.get("snapshot", {})
        attempts.append(Attempt(
            number=meta.get("number", 1),
            tier_index=meta.get("tier", 0),
            model=meta.get("model", ""),
            conversation=env.conversation_id,
            prompt="",
            envelope=env,
            report=report,
            changed=meta.get("changed", []),
            restored=meta.get("restored", []),
            snapshot_label=snap_meta.get("label", ""),
            snapshot_commit=snap_meta.get("commit", ""),
            snapshot_tree=snap_meta.get("tree", ""),
        ))
        
    if not attempts:
        tier_idx = 0
        attempt_num = 1
        conversation = None
    else:
        last = attempts[-1]
        tier_idx = last.tier_index
        attempt_num = last.number + 1
        conversation = last.conversation
        
    inputs_dict = {}
    for ref in spec.inputs:
        ref_state = TaskState(state.root, spec.dir, ref.task)
        res = ref_state.load_result()
        if res:
            inputs_dict[ref.task] = res
            
    brief_text = ""
    if state.brief_path.exists():
        brief_text = state.brief_path.read_text(encoding="utf-8")
            
    return _run_loop(
        spec=spec,
        executor=executor,
        state=state,
        schema_dir=schema_dir,
        clock=clock,
        initial_prompt=message,
        start_tier=tier_idx,
        start_attempt=attempt_num,
        attempts=attempts,
        start_conversation=conversation,
        brief_text=brief_text,
        inputs=inputs_dict,
        semantic_kind="semantic",
    )
