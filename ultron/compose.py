import json
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ultron import policy
from ultron.check import CheckReport, DIFF_LIMIT_PREFIX
from ultron.spec import Spec


@dataclass(frozen=True)
class AttemptSummary:
    number: int
    model: str
    summary: str
    violations: tuple[str, ...]
    gate_tail: tuple[str, ...]


def _compose_blocks(spec: Spec, brief_text: str, inputs: Mapping[str, dict]) -> list[str]:
    blocks = [brief_text.rstrip()]
    
    for inp in spec.inputs:
        lines = [f"ДАНО ИЗ {inp.task}"]
        task_res = inputs.get(inp.task, {})
        for field in inp.fields:
            val = task_res.get(field)
            if val is None:
                role_fields = task_res.get("role_fields", {})
                val = role_fields.get(field)
                
            if val is None:
                lines.append(f"{field}: нет")
            elif isinstance(val, (list, dict)):
                lines.append(f"{field}: {json.dumps(val, ensure_ascii=False)}")
            else:
                lines.append(f"{field}: {val}")
        blocks.append("\n".join(lines))
        
    if not spec.writes:
        blocks.append(f"ОБЪЁМ — {policy.READONLY_BAN}")
    else:
        writes_str = ", ".join(spec.scope.write) if spec.scope.write else "любые файлы в рабочем каталоге"
        scope_str = f"ОБЪЁМ — менять только: {writes_str}."
        if spec.scope.forbid:
            scope_str += f" Не трогать: {', '.join(spec.scope.forbid)}."
        blocks.append(scope_str)
        
    if spec.writes and spec.acceptance:
        blocks.append(policy.GATE_NOTE)
        
    blocks.append(spec.role_policy.answer_template)
    
    return blocks


def compose_prompt(spec: Spec, brief_text: str, inputs: Mapping[str, dict]) -> str:
    blocks = _compose_blocks(spec, brief_text, inputs)
    blocks.append(policy.GIT_BAN)
    return "\n\n".join(blocks)


def retry_message(report: CheckReport, spec: Spec, restored: Sequence[str] = ()) -> str:
    blocks = ["Приёмка не пройдена."]
    
    for g in report.gate:
        if not g.ok:
            blocks.append(f"Гейт: {g.cmd} — код выхода {g.exit_code}, ожидался {g.expected}.")
            if g.tail:
                blocks.append("Хвост вывода:\n" + "\n".join(g.tail))
    
    if restored:
        diff_size_viol = any(v.startswith(DIFF_LIMIT_PREFIX) for v in report.scope)
        if diff_size_viol:
            limit = spec.scope.max_diff_lines
            blocks.append(f"Файлы восстановлены из исходного состояния. Внеси только точечные правки: не больше {limit} строк diff. Переписывать файл целиком нельзя.")
        else:
            blocks.append(f"Файлы вне объёма восстановлены оркестратором из исходного состояния: {', '.join(restored)}. Не трогай их.")
    elif report.scope:
        scope_lines = ["Нарушения объёма — откати эти файлы:"]
        for s in report.scope:
            scope_lines.append(s)
        if not spec.writes:
            scope_lines.append(policy.READONLY_BAN)
        blocks.append("\n".join(scope_lines))
        
    if report.contract:
        blocks.append(f"Ответ не по схеме: {'; '.join(report.contract)}")
        
    if report.anchors:
        anchors_block = ["Якоря не подтвердились:"]
        anchors_block.extend(report.anchors)
        anchors_block.append("Для каждого: исправь путь и номер строки по файлу или сними утверждение. Выдумывать якорь нельзя; если места в коде нет — пиши «не найдено».")
        blocks.append("\n".join(anchors_block))
        
    if not spec.writes:
        fix_msg = "Файлы не менять, поправь ответ."
    else:
        writes = ", ".join(spec.scope.write) if spec.scope.write else "рабочий каталог"
        fix_msg = f"Исправь причину в разрешённых файлах ({writes})."
        if spec.scope.forbid:
            fix_msg += f" {', '.join(spec.scope.forbid)} не трогать."
    blocks.append(fix_msg)
    
    blocks.append(policy.GIT_BAN)
    blocks.append("Ответ строго по схеме: changed_files — все тронутые файлы за все попытки.")
    
    return "\n\n".join(blocks)


def report_only_message(spec: Spec) -> str:
    return "\n\n".join([
        "Код принят: гейт и объём в порядке. Отчёт не разобран. Пришли только отчёт строго по схеме, файлы не меняй.",
        policy.GIT_BAN
    ])


def escalation_prompt(spec: Spec, brief_text: str, inputs: Mapping[str, dict], previous: Sequence[AttemptSummary], previous_patch: pathlib.Path | None = None) -> str:
    blocks = _compose_blocks(spec, brief_text, inputs)
    
    if previous:
        prev_lines = ["ПРЕДЫДУЩИЕ ПОПЫТКИ"]
        for p in previous:
            viol_str = "; ".join(p.violations)
            prev_lines.append(f"#{p.number} ({p.model}): {p.summary}; нарушения: {viol_str}")
            if p.gate_tail:
                prev_lines.append("Хвост вывода:")
                prev_lines.extend(p.gate_tail)
        blocks.append("\n".join(prev_lines))
        
    if previous_patch:
        blocks.append(f"Дерево возвращено к исходному состоянию. Твои прошлые изменения в {previous_patch}.")
        
    blocks.append(policy.GIT_BAN)
    return "\n\n".join(blocks)
