from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ultron import policy


class SpecError(ValueError):
    """Raised when a spec is invalid."""


@dataclass(frozen=True)
class Scope:
    write: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class InputRef:
    task: str
    fields: tuple[str, ...] = ("summary",)


@dataclass(frozen=True)
class Acceptance:
    cmd: str
    expect_exit: int = 0


@dataclass(frozen=True)
class ExecutorOpts:
    model: str | None = None
    effort: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class TaskBudget:
    attempts_per_tier: int
    tiers: tuple[str, ...]
    executor_tokens: int
    minutes: int


@dataclass(frozen=True)
class Workspace:
    mode: str = "inplace"
    seed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Spec:
    id: str
    role: str
    dir: Path
    brief: Path
    goal: str
    scope: Scope
    inputs: tuple[InputRef, ...]
    acceptance: tuple[Acceptance, ...]
    budget: TaskBudget
    executor: ExecutorOpts
    workspace: Workspace

    @property
    def role_policy(self) -> policy.Role:
        return policy.ROLES[self.role]

    @property
    def writes(self) -> bool:
        return self.role_policy.writes and self.executor.mode != "plan"


def _check_keys(data: dict, allowed: set[str], path: str = "") -> None:
    for k in data:
        if k not in allowed:
            raise SpecError(f"Неизвестный ключ: {path}{k}")


def parse_spec(data: dict, base_dir: Path) -> Spec:
    """Parse a task spec from a dictionary."""
    _check_keys(
        data,
        {"id", "role", "dir", "brief", "goal", "scope", "inputs", "acceptance", "budget", "executor", "workspace"}
    )
    
    for req in ("id", "role", "dir", "brief"):
        if req not in data:
            raise SpecError(f"Отсутствует обязательное поле: {req}")

    spec_id = data["id"]
    if not isinstance(spec_id, str) or not re.match(r"^[A-Za-z0-9._-]+$", spec_id):
        raise SpecError(f"Неверный id: {spec_id}")

    role = data["role"]
    if role not in policy.ROLES:
        raise SpecError(f"Неизвестная role: {role}")

    dir_path = Path(data["dir"])
    if not dir_path.is_absolute() or not dir_path.is_dir():
        raise SpecError(f"dir должен быть абсолютным существующим каталогом: {dir_path}")

    brief_path = Path(data["brief"])
    if not brief_path.is_absolute():
        brief_path = base_dir / brief_path
    if not brief_path.is_file():
        raise SpecError(f"brief должен быть существующим файлом: {brief_path}")

    goal = data.get("goal", "")

    scope_data = data.get("scope", {})
    _check_keys(scope_data, {"write", "forbid"}, "scope.")
    scope = Scope(
        write=tuple(scope_data.get("write", ())),
        forbid=tuple(scope_data.get("forbid", ())),
    )

    inputs_data = data.get("inputs", [])
    inputs = []
    for i, inp in enumerate(inputs_data):
        _check_keys(inp, {"task", "fields"}, f"inputs[{i}].")
        inputs.append(InputRef(
            task=inp["task"],
            fields=tuple(inp.get("fields", ("summary",)))
        ))

    acceptance_data = data.get("acceptance", [])
    acceptance = []
    for i, acc in enumerate(acceptance_data):
        _check_keys(acc, {"cmd", "expect_exit"}, f"acceptance[{i}].")
        acceptance.append(Acceptance(
            cmd=acc["cmd"],
            expect_exit=acc.get("expect_exit", 0)
        ))

    executor_data = data.get("executor", {})
    _check_keys(executor_data, {"model", "effort", "mode"}, "executor.")
    executor = ExecutorOpts(
        model=executor_data.get("model"),
        effort=executor_data.get("effort"),
        mode=executor_data.get("mode"),
    )

    budget_data = data.get("budget", {})
    _check_keys(budget_data, {"attempts_per_tier", "tiers", "executor_tokens", "minutes"}, "budget.")
    
    tiers = tuple(budget_data.get("tiers", policy.ROLES[role].tiers))
    if executor.model:
        tiers = (executor.model,) + tiers[1:]

    budget = TaskBudget(
        attempts_per_tier=budget_data.get("attempts_per_tier", policy.DEFAULT_BUDGET.attempts_per_tier),
        tiers=tiers,
        executor_tokens=budget_data.get("executor_tokens", policy.DEFAULT_BUDGET.executor_tokens),
        minutes=budget_data.get("minutes", policy.DEFAULT_BUDGET.minutes),
    )

    workspace_data = data.get("workspace", {})
    _check_keys(workspace_data, {"mode", "seed"}, "workspace.")
    mode = workspace_data.get("mode", "inplace")
    if mode not in {"inplace", "worktree"}:
        raise SpecError(f"Неизвестный mode: {mode}")
    workspace = Workspace(
        mode=mode,
        seed=tuple(workspace_data.get("seed", ())),
    )

    return Spec(
        id=spec_id,
        role=role,
        dir=dir_path,
        brief=brief_path,
        goal=goal,
        scope=scope,
        inputs=tuple(inputs),
        acceptance=tuple(acceptance),
        budget=budget,
        executor=executor,
        workspace=workspace,
    )


def load_spec(path: Path) -> Spec:
    """Load and parse a task spec from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return parse_spec(data, path.parent)
