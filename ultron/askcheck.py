"""Verify what a free-text `ask` answer claims about the code.

Two cheap checks, no executor involved: every `path:line` anchor must point at
an existing line, and every identifier quoted in backticks must occur in at
least one of the files the answer anchors. A reference that exists proves
nothing about the conclusion drawn from it; a reference that does not exist
proves the answer is wrong there.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

_ANCHOR = re.compile(r"(?<![\w/.-])(?:file://)?((?:/|\./)?[\w@+-][\w./@+-]*\.[A-Za-z]\w{0,9})(?::|#L)(\d+)")
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")
_LITERALS = {"true", "false", "null", "none", "nil", "self", "this", "void", "int", "str", "bool"}
_IDENT = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")


@dataclass(frozen=True)
class AnswerCheck:
    anchors: int
    symbols: int
    problems: tuple[str, ...]


def _relative(root: pathlib.Path, raw: str) -> pathlib.Path | None:
    path = pathlib.Path(raw)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve())
        except ValueError:
            return None
    return path


def _looks_like_code_name(name: str) -> bool:
    """Backticks already mark code; only literals and very short tokens are skipped."""
    bare = name.rstrip("()")
    return len(bare) >= 3 and bare.lower() not in _LITERALS


def _word(name: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")


def _name_occurs(bare: str, bodies: list[str]) -> bool:
    """A plain name must occur as a word. A qualified one (`Class.method`) is
    rarely written that way in code, so it passes when every part occurs."""
    if any(_word(bare).search(body) for body in bodies):
        return True
    parts = bare.split(".")
    return len(parts) > 1 and all(any(_word(part).search(body) for body in bodies) for part in parts)


def check_answer(text: str, root: pathlib.Path) -> AnswerCheck:
    problems: list[str] = []
    files: dict[str, str] = {}
    seen: set[tuple[str, int]] = set()
    for raw, line_text in _ANCHOR.findall(text):
        rel = _relative(root, raw)
        if rel is None:
            continue
        key = (str(rel), int(line_text))
        if key in seen:
            continue
        seen.add(key)
        if str(rel) not in files:
            try:
                files[str(rel)] = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                files[str(rel)] = ""
                problems.append(f"файла нет: {rel}")
                continue
        body = files[str(rel)]
        total = body.count("\n") + 1
        if body and key[1] > total:
            problems.append(f"строки нет: {rel}:{key[1]} (в файле {total})")

    names: list[str] = []
    for quoted in _BACKTICK.findall(text):
        quoted = quoted.strip()
        if _IDENT.match(quoted) and _looks_like_code_name(quoted) and quoted not in names and not (root / quoted).exists():
            names.append(quoted)
    bodies = [b for b in files.values() if b]
    checked = 0
    if bodies:
        for name in names:
            checked += 1
            bare = name.rstrip("()")
            if not _name_occurs(bare, bodies):
                problems.append(f"имя `{name}` не встречается в файлах, на которые ссылается ответ")
    return AnswerCheck(anchors=len(seen), symbols=checked, problems=tuple(problems))
