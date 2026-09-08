import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ultron import policy
from ultron.executor import Envelope
from ultron.spec import Spec


@dataclass(frozen=True)
class GateRun:
    cmd: str
    exit_code: int
    expected: int
    tail: tuple[str, ...]
    log: str

    @property
    def ok(self) -> bool:
        return self.exit_code == self.expected


@dataclass(frozen=True)
class CheckReport:
    contract: tuple[str, ...]
    scope: tuple[str, ...]
    gate: tuple[GateRun, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.contract and not self.scope and all(g.ok for g in self.gate)

    @property
    def violations(self) -> tuple[str, ...]:
        viol = list(self.contract) + list(self.scope)
        for g in self.gate:
            if not g.ok:
                viol.append(f"гейт {g.cmd}: код {g.exit_code}, ожидался {g.expected}")
        return tuple(viol)


def load_role_schema(role: str, schema_dir: Path | None = None) -> dict:
    if schema_dir is None:
        schema_dir = Path(__file__).parent / "schemas" / "roles"
    with open(schema_dir / f"{role}.json", "r", encoding="utf-8") as f:
        return json.load(f)


def check_contract(env: Envelope, role: policy.Role, schema: dict) -> tuple[list[str], list[str]]:
    violations = []
    warnings = []

    if env.status != "SUCCESS":
        violations.append(f"agy status={env.status}")

    if env.denied_actions:
        names = ", ".join(env.denied_actions)
        violations.append(f"исполнитель без прав: {names}")

    so = env.structured_output
    if so is None:
        violations.append("нет structured_output: ответ не по схеме")
        return violations, warnings

    missing = [f for f in schema.get("required", []) if f not in so]
    if missing:
        violations.append(f"нет обязательных полей: {', '.join(missing)}")

    st = so.get("status")
    if st is not None and st not in policy.EXECUTOR_STATUSES:
        violations.append(f"status={st} вне done/partial/failed/blocked")

    for field, cap in policy.CAPS.items():
        if field == "summary_chars":
            val = so.get("summary", "")
            if isinstance(val, str) and len(val) > cap:
                warnings.append(f"summary: {len(val)} при лимите {cap}")
        else:
            val = so.get(field)
            if isinstance(val, list) and len(val) > cap:
                warnings.append(f"{field}: {len(val)} при лимите {cap}")

    if role.name == "code" and st == "done":
        changed = so.get("changed_files", [])
        patch = so.get("patch", "")
        if not changed and not patch:
            warnings.append("status=done, но changed_files и patch пусты")

    return violations, warnings


def check_scope(changed: Sequence[str], spec: Spec) -> list[str]:
    if not changed:
        return []

    if not spec.writes:
        return [f"роль {spec.role} без записи, но изменено: {', '.join(changed)}"]

    violations = []
    for path in changed:
        allowed = True
        if spec.scope.write:
            allowed = False
            for w in spec.scope.write:
                w_dir = w if w.endswith("/") else w + "/"
                if path == w or path.startswith(w_dir):
                    allowed = True
                    break

        if allowed and spec.scope.forbid:
            for f in spec.scope.forbid:
                f_dir = f if f.endswith("/") else f + "/"
                if path == f or path.startswith(f_dir):
                    allowed = False
                    break

        if not allowed:
            violations.append(f"вне разрешённого объёма: {path}")

    return violations


def run_gate(spec: Spec, cwd: Path | None = None) -> list[GateRun]:
    runs = []
    for acc in spec.acceptance:
        proc = subprocess.run(
            acc.cmd,
            shell=True,
            cwd=str(cwd) if cwd else str(spec.dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

        log_bytes = proc.stdout
        if len(log_bytes) > policy.GATE_LOG_MAX_BYTES:
            log_bytes = log_bytes[:policy.GATE_LOG_MAX_BYTES]

        log = log_bytes.decode("utf-8", errors="replace")
        lines = log.splitlines()
        tail = tuple(lines[-policy.GATE_TAIL_LINES:]) if lines else ()

        ok = proc.returncode == acc.expect_exit
        runs.append(GateRun(
            cmd=acc.cmd,
            exit_code=proc.returncode,
            expected=acc.expect_exit,
            tail=tail,
            log=log
        ))

        if not ok:
            break

    return runs


def check_all(env: Envelope, spec: Spec, schema: dict, changed: Sequence[str], cwd: Path | None = None) -> CheckReport:
    contract_v, warnings = check_contract(env, spec.role_policy, schema)

    scope_v = check_scope(changed, spec)

    gate_runs = []
    if spec.acceptance:
        gate_runs = run_gate(spec, cwd)

    return CheckReport(
        contract=tuple(contract_v),
        scope=tuple(scope_v),
        gate=tuple(gate_runs),
        warnings=tuple(warnings)
    )
