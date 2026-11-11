"""`ultron usage`: executor calls and tokens over the subscription windows.

Reads every project's journal under the state root. Records written before
timestamps existed have no `_meta.at` and are counted separately.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Iterator

from ultron import policy


def read_journals(root: pathlib.Path) -> Iterator[dict]:
    if not root.exists():
        return
    for journal in sorted(root.glob("*/journal.jsonl")):
        try:
            lines = journal.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _age_seconds(record: dict, now: datetime) -> float | None:
    at = (record.get("_meta") or {}).get("at")
    if not isinstance(at, str):
        return None
    try:
        return (now - datetime.fromisoformat(at)).total_seconds()
    except (ValueError, TypeError):
        return None


def summarize(root: pathlib.Path, now: datetime | None = None) -> dict:
    """Per window: {model: {"calls", "input", "output"}}; plus count of undated records."""
    now = now or datetime.now(timezone.utc)
    windows: dict[str, dict[str, dict[str, int]]] = {name: {} for name, _ in policy.USAGE_WINDOWS}
    week = policy.USAGE_WINDOWS[-1][1]
    decisions: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    cost = {"claude_chars": 0, "executor_tokens": 0, "unverified": 0}
    undated = 0
    for record in read_journals(root):
        meta = record.get("_meta") or {}
        age = _age_seconds(record, now)
        if age is None:
            undated += 1
            continue
        if meta.get("role") == "decision":
            if 0 <= age <= week:
                decisions[meta.get("to") or "?"] = decisions.get(meta.get("to") or "?", 0) + 1
            continue
        usage = record.get("usage") or {}
        model = meta.get("model") or "?"
        if 0 <= age <= week:
            verdict = meta.get("verdict") or "?"
            outcomes[verdict] = outcomes.get(verdict, 0) + 1
            cost["claude_chars"] += int(meta.get("claude_chars") or 0)
            cost["executor_tokens"] += int(usage.get("total_tokens") or 0)
            cost["unverified"] += int(meta.get("unverified") or 0)
        for name, span in policy.USAGE_WINDOWS:
            if 0 <= age <= span:
                row = windows[name].setdefault(model, {"calls": 0, "input": 0, "output": 0})
                row["calls"] += 1
                row["input"] += int(usage.get("input_tokens") or 0)
                row["output"] += int(usage.get("output_tokens") or 0)
    return {"windows": windows, "undated": undated, "decisions": decisions, "outcomes": outcomes, "cost": cost}


def render(summary: dict) -> str:
    lines: list[str] = []
    for name, _ in policy.USAGE_WINDOWS:
        rows = summary["windows"][name]
        calls = sum(r["calls"] for r in rows.values())
        lines.append(f"За {name}: вызовов {calls}")
        for model in sorted(rows, key=lambda m: -rows[m]["calls"]):
            r = rows[model]
            lines.append(f"  {model}: {r['calls']} выз., вход {r['input']}, выход {r['output']}")
    names = {"self": "Claude сам", "subagent": "субагент Claude", "flash": "Gemini Flash", "argon": "Gemini Argon"}
    if summary.get("decisions"):
        lines.append("Решения за 7 дн: " + ", ".join(f"{names.get(k, k)} — {v}" for k, v in sorted(summary["decisions"].items(), key=lambda kv: -kv[1])))
    if summary.get("outcomes"):
        lines.append("Исходы вызовов за 7 дн: " + ", ".join(f"{k} — {v}" for k, v in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1])))
    cost = summary.get("cost") or {}
    if cost.get("claude_chars"):
        lines.append(f"Цена для Claude за 7 дн: ≈{cost['claude_chars'] // 4} токенов на брифы и ответы против {cost['executor_tokens']} токенов исполнителя")
    if cost.get("unverified"):
        lines.append(f"Неподтверждённых мест в ответах ask за 7 дн: {cost['unverified']}")
    if summary["undated"]:
        lines.append(f"Без даты (записаны до учёта времени): {summary['undated']}")
    lines.append("Лимиты подписки Google не публикует: это только наш счёт вызовов.")
    return "\n".join(lines)
