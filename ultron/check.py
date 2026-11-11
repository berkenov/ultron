import json
import re
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
    anchors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.contract and not self.scope and not self.anchors and all(g.ok for g in self.gate)

    @property
    def violations(self) -> tuple[str, ...]:
        viol = list(self.contract) + list(self.scope) + list(self.anchors)
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


DIFF_LIMIT_PREFIX = "правка больше лимита: "


def check_diff_size(patch: str, limit: int | None) -> list[str]:
    if limit is None or not patch:
        return []

    lines = 0
    for line in patch.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            lines += 1

    if lines > limit:
        return [f"{DIFF_LIMIT_PREFIX}{lines} строк при лимите {limit} — задача точечная, переписывать файл нельзя"]
    return []


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


def _collect_anchor_refs(so: dict, role: policy.Role) -> list[tuple[str, str, str | None]]:
    anchors = []
    def add(items: list | None, source: str, get_exc: bool = False):
        if not isinstance(items, list): return
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("where"), str):
                anchors.append((it["where"], source, it.get("excerpt") if get_exc else None))

    add(so.get("evidence"), "evidence", get_exc=True)
    if role.name == "review":
        add(so.get("verdicts"), "verdicts")
        add(so.get("risks"), "risks")
    elif role.name == "explore":
        add(so.get("facts"), "facts")
    elif role.name == "verify":
        add(so.get("failures"), "failures")
    return anchors

def _parse_anchor(where: str) -> tuple[str, int | None] | None:
    w = where.strip()
    if not w or w == "-" or w.lower() == "не найдено" or " " in w or w.startswith("~") or w.startswith("/"):
        return None
    m = re.match(r"^[\w./@+-]+(?::(\d+)(?:\s*[-–]\s*(\d+))?)?$", w)
    if not m:
        return None
    return w.split(":", 1)[0], int(m.group(1)) if m.group(1) else None

def _path_kind(root: Path, path: str, cache: dict) -> str | None:
    if path not in cache:
        target = root / path
        try:
            if not target.exists(): cache[path] = None
            elif target.is_dir(): cache[path] = "dir"
            else: cache[path] = "file"
        except Exception:
            cache[path] = None
    return cache[path]

def _read_lines(root: Path, path: str, cache: dict) -> list[str] | None:
    if path not in cache:
        try:
            cache[path] = (root / path).read_text(encoding="utf-8").splitlines()
        except Exception:
            cache[path] = None
    return cache[path]

def _excerpt_present(lines: list[str], line: int | None, excerpt: str) -> bool:
    norm = lambda s: re.sub(r'\s+', ' ', s).strip()
    tgt = next((norm(el) for el in excerpt.splitlines() if len(norm(el)) >= 12), None)
    if not tgt:
        return True
    start = max(0, line - 4) if line is not None else 0
    end = min(len(lines), line + 3) if line is not None else len(lines)
    return any(tgt in norm(sl) for sl in lines[start:end])

def _missing_evidence(so: dict, refs: list[dict]) -> list[str]:
    ev_items = [r for r in refs if r["source"] == "evidence"]
    def covers(path: str, line: int | None) -> bool:
        for e in ev_items:
            if e["path"] == path and (line is None or e["line"] is None or abs(e["line"] - line) <= 3):
                return True
        return False
    v = []
    for items, is_verdict in [(so.get("verdicts", []), True), (so.get("risks", []), False)]:
        if not isinstance(items, list): continue
        for it in items:
            if not isinstance(it, dict): continue
            if is_verdict and it.get("verdict") not in ("false", "partially"): continue
            w = it.get("where")
            if not isinstance(w, str): continue
            p = _parse_anchor(w)
            if p and not covers(p[0], p[1]):
                v.append(f"нет выдержки в evidence для {w.strip()}")
    return v

def check_anchors(so: dict, role: policy.Role, root: Path) -> list[str]:
    if role.name == "code": return []
    refs = []
    for w, src, exc in _collect_anchor_refs(so, role):
        p = _parse_anchor(w)
        if p: refs.append({"where": w.strip(), "path": p[0], "line": p[1], "source": src, "excerpt": exc})
    
    kind_cache: dict[str, str | None] = {}
    lines_cache: dict[str, list[str] | None] = {}
    v_set, v = set(), []
    
    def add(msg: str):
        if msg not in v_set:
            v_set.add(msg)
            v.append(msg)
            
    for r in refs:
        kind = _path_kind(root, r["path"], kind_cache)
        if kind is None:
            add(f"якорь: файла нет: {r['where']}")
        elif kind == "dir":
            if r["line"] is not None:
                add(f"якорь: у каталога нет строк: {r['where']}")
        elif kind == "file":
            lines = _read_lines(root, r["path"], lines_cache)
            if lines is None:
                add(f"якорь: файла нет: {r['where']}")
            elif r["line"] is not None and r["line"] > len(lines):
                add(f"якорь: строка {r['line']} за пределами файла ({len(lines)} строк): {r['where']}")
            elif r["source"] == "evidence" and r["excerpt"] and isinstance(r["excerpt"], str):
                if not _excerpt_present(lines, r["line"], r["excerpt"]):
                    add(f"якорь: выдержка не совпадает с файлом у {r['where']}")
                    
    if role.name == "review":
        for msg in _missing_evidence(so, refs): add(msg)
        
    return v[:20] + [f"…и ещё {len(v) - 20}"] if len(v) > 20 else v


def check_all(env: Envelope, spec: Spec, schema: dict, changed: Sequence[str], cwd: Path | None = None) -> CheckReport:
    contract_v, warnings = check_contract(env, spec.role_policy, schema)

    scope_v = check_scope(changed, spec)

    gate_runs = []
    if spec.acceptance:
        gate_runs = run_gate(spec, cwd)
        
    anchors_v = []
    if env.structured_output is not None:
        anchors_v = check_anchors(env.structured_output, spec.role_policy, cwd or spec.dir)

    return CheckReport(
        contract=tuple(contract_v),
        scope=tuple(scope_v),
        gate=tuple(gate_runs),
        anchors=tuple(anchors_v),
        warnings=tuple(warnings)
    )
