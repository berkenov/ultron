"""Learned executor limits: quota failures in, working thresholds out.

Per model family (the model name without its effort suffix) the state file
`limits.json` keeps the quota failures seen, with usage over both subscription
windows at that moment. A failure is attributed to a window by what happens
next: the first call after the block succeeds -> it was the short window;
it fails again -> the long one. The working threshold is a share of the lowest
attributed usage, creeps upward while nothing fails, and a ceiling that was
exceeded without a failure is dropped. Before the first failure nothing is
enforced.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ultron import policy
from ultron.executor import Envelope
from ultron.usage import read_journals

FILE_NAME = "limits.json"
SHORT, LONG = (name for name, _ in policy.USAGE_WINDOWS)
_EFFORT = re.compile(r"-(low|medium|high|xhigh|max|thinking)$")
_QUOTA = re.compile(policy.QUOTA_PATTERN, re.IGNORECASE)


def family(model: str) -> str:
    return _EFFORT.sub("", model or "?")


def is_quota_failure(env: Envelope) -> bool:
    """A non-successful envelope whose error text speaks of quota or rate limits."""
    if env.status == "SUCCESS":
        return False
    parts = [str(env.raw.get("error") or ""), str(env.raw.get("message") or ""), *env.noise]
    return bool(_QUOTA.search("\n".join(parts)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def load(root: pathlib.Path) -> dict:
    try:
        data = json.loads((root / FILE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(root: pathlib.Path, data: dict) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / FILE_NAME).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def window_usage(root: pathlib.Path, fam: str, now: datetime) -> dict:
    """{window: {"calls", "tokens"}} for one family, from the journals."""
    out = {name: {"calls": 0, "tokens": 0} for name, _ in policy.USAGE_WINDOWS}
    for record in read_journals(root):
        meta = record.get("_meta") or {}
        at = _parse(meta.get("at"))
        if at is None or family(meta.get("model") or "") != fam:
            continue
        age = (now - at).total_seconds()
        tokens = int((record.get("usage") or {}).get("total_tokens") or 0)
        for name, span in policy.USAGE_WINDOWS:
            if 0 <= age <= span:
                out[name]["calls"] += 1
                out[name]["tokens"] += tokens
    return out


def thresholds(entry: dict, now: datetime) -> dict:
    """{window: {"calls", "tokens", "based_on"}} for windows with a learned ceiling."""
    out: dict[str, dict] = {}
    peaks = entry.get("peaks") or {}
    for window, _ in policy.USAGE_WINDOWS:
        live = []
        for obs in entry.get("failures") or []:
            at = _parse(obs.get("at"))
            if obs.get("window") != window or at is None:
                continue
            days = (now - at).total_seconds() / 86400
            seen = (obs.get("usage") or {}).get(window) or {}
            exceeded = (peaks.get(window) or {}).get("calls", 0) > seen.get("calls", 0)
            if days > policy.LIMIT_FORGET_DAYS or exceeded or not seen.get("calls"):
                continue
            share = min(policy.LIMIT_GROWTH_MAX, policy.LIMIT_SAFETY + policy.LIMIT_GROWTH_PER_DAY[window] * days)
            live.append((seen["calls"] * share, seen.get("tokens", 0) * share))
        if live:
            calls, tokens = min(live)
            out[window] = {"calls": max(1, int(calls)), "tokens": int(tokens), "based_on": len(live)}
    return out


@dataclass(frozen=True)
class Budget:
    family: str
    mode: str  # free | saving | empty
    reason: str
    blocked_until: datetime | None
    usage: dict
    limits: dict

    @property
    def allowed(self) -> bool:
        return self.mode != "empty"


def budget(root: pathlib.Path, model: str, now: datetime | None = None) -> Budget:
    now = now or _now()
    fam = family(model)
    entry = load(root).get(fam) or {}
    usage = window_usage(root, fam, now)
    limits = thresholds(entry, now)
    until = _parse(entry.get("blocked_until"))
    if until and until > now:
        return Budget(fam, "empty", f"отказ по квоте, пауза до {until.astimezone():%d.%m %H:%M}", until, usage, limits)
    share, where = 0.0, ""
    for window, lim in limits.items():
        for unit in ("calls", "tokens"):
            if lim[unit]:
                part = usage[window][unit] / lim[unit]
                if part > share:
                    share, where = part, f"{window}: {usage[window][unit]} из {lim[unit]} ({'вызовы' if unit == 'calls' else 'токены'})"
    if not limits:
        return Budget(fam, "free", "лимит ещё не встречен, порога нет", None, usage, limits)
    mode = "empty" if share >= 1 else "saving" if share >= policy.SAVING_SHARE else "free"
    return Budget(fam, mode, where, None, usage, limits)


def record(root: pathlib.Path, model: str, env: Envelope, now: datetime | None = None) -> bool:
    """Learn from one finished call; returns True when it was a quota failure."""
    now = now or _now()
    fam = family(model)
    data = load(root)
    entry = data.setdefault(fam, {})
    failures = entry.setdefault("failures", [])
    pending = next((f for f in failures if f.get("window") is None), None)
    usage = window_usage(root, fam, now)

    if is_quota_failure(env):
        strikes = int(entry.get("strikes") or 0) + 1
        if pending is not None and strikes > 1:
            pending["window"] = LONG  # still refused after the short window passed
        elif pending is None and strikes == 1:
            failures.append({"at": now.isoformat(timespec="seconds"), "window": None, "usage": usage})
        hours = min(policy.QUOTA_BLOCK_MAX_HOURS, policy.QUOTA_BLOCK_HOURS * 2 ** (strikes - 1))
        entry["strikes"] = strikes
        entry["blocked_until"] = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
        entry["peaks"] = {}
        _save_raw_failure(root, fam, env, now)
        _save(root, data)
        return True

    if env.status == "SUCCESS":
        if pending is not None:
            pending["window"] = SHORT if int(entry.get("strikes") or 0) <= 1 else LONG
        entry["strikes"] = 0
        entry["blocked_until"] = None
        peaks = entry.setdefault("peaks", {})
        for window, _ in policy.USAGE_WINDOWS:
            seen = usage[window]["calls"] + 1  # this call is journaled after the check
            if seen > (peaks.get(window) or {}).get("calls", 0):
                peaks[window] = {"calls": seen}
        _save(root, data)
    return False


def _save_raw_failure(root: pathlib.Path, fam: str, env: Envelope, now: datetime) -> None:
    """Keep the refusal verbatim: its exact shape is how detection gets tuned."""
    try:
        path = root / "quota-failures" / f"{now:%Y%m%dT%H%M%SZ}-{fam}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"raw": env.raw, "noise": list(env.noise), "status": env.status}, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


MODE_NAMES = {"free": "свободно", "saving": "экономно", "empty": "пусто"}


def render(budgets: list[Budget]) -> str:
    lines = []
    for b in budgets:
        lines.append(f"{b.family}: {MODE_NAMES[b.mode]} — {b.reason}")
        for window, _ in policy.USAGE_WINDOWS:
            u, lim = b.usage[window], b.limits.get(window)
            learned = f"порог {lim['calls']} выз. (по {lim['based_on']} отказам)" if lim else "порог не выучен"
            lines.append(f"  за {window}: {u['calls']} выз., {u['tokens']} токенов · {learned}")
    lines.append("свободно — отдавать всё по таблице; экономно — только чтение на Flash; пусто — Claude работает сам.")
    return "\n".join(lines)
