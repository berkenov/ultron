#!/usr/bin/env python3
"""worker.py — запуск исполнителя (agy) по контракту gemini-delegate v2.

run   собирает команду agy по роли, запускает с --dangerously-skip-permissions,
      пишет журнал, проверяет ответ по контракту и печатает компактный
      TaskResult. Сырой вывод исполнителя в терминал не идёт.
show  перерисовывает результат из журнала (последний или по conversation_id),
      показывает одно доказательство целиком (--evidence) или сырой
      structured_output (--raw).

Коды выхода: 0 — контракт соблюдён; 2 — нарушен, ответ принимать нельзя;
1 — ошибка запуска.
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time

SKILL_DIR = pathlib.Path(__file__).resolve().parent
SCHEMAS = SKILL_DIR / "schemas"
STATUSES = ("done", "partial", "failed", "blocked")
ROLES = {
    "explore": {"model": "gemini-3.8-flash-high", "writes": False},
    "code": {"model": "gemini-3.1-pro-high", "writes": True},
    "verify": {"model": "gemini-3.8-flash-high", "writes": False},
    "review": {"model": "gemini-3.1-pro-high", "writes": False},
}
CAPS = {"summary": 700, "evidence": 12, "open_questions": 5, "map": 40, "facts": 20,
        "changed_files": 60, "commands": 10, "failures": 20, "warnings": 10,
        "verdicts": 30, "risks": 15}
LIST_LIMIT = 25


def cut(s, n):
    s = " ".join(str(s if s is not None else "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_tokens(n):
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def parse_duration(s):
    m = re.fullmatch(r"(\d+)([smh]?)", s.strip())
    if not m:
        sys.exit(f"неверный таймаут: {s} (пример: 20m, 300s, 1h)")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2) or "s"]


def load_schema(role):
    return json.loads((SCHEMAS / f"{role}.json").read_text(encoding="utf-8"))


def git_status(d):
    p = subprocess.run(["git", "-C", str(d), "status", "--porcelain"], capture_output=True, text=True)
    return set(p.stdout.splitlines()) if p.returncode == 0 else None


def git_ignored(d, path):
    p = subprocess.run(["git", "-C", str(d), "check-ignore", "-q", str(path)], capture_output=True)
    return p.returncode == 0


def parse_agy_output(text):
    """Последняя JSON-строка с conversation_id/status — ответ agy; остальное — шум."""
    obj, noise = None, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("{"):
            try:
                cand = json.loads(s)
            except json.JSONDecodeError:
                noise.append(line)
                continue
            if isinstance(cand, dict) and ("conversation_id" in cand or "status" in cand):
                obj = cand
                continue
        if s:
            noise.append(line)
    return obj, noise


def structured(agy):
    so = agy.get("structured_output")
    if isinstance(so, dict):
        return so
    resp = agy.get("response")
    if isinstance(resp, str) and resp.strip().startswith("{"):
        try:
            return json.loads(resp)
        except json.JSONDecodeError:
            return None
    return None


# ---------- контракт ----------

def brief_checks(role, brief, mode):
    low = brief.lower()
    w = []
    writes = ROLES[role]["writes"] and mode != "plan"
    if writes and "коммит" not in low and "commit" not in low:
        w.append("в брифе нет запрета на коммиты")
    if not writes and "не менять" not in low and "не трогать" not in low:
        w.append("в брифе роли без записи нет фразы «файлы не менять»")
    return w


def validate_core(agy, res):
    v = []
    if agy.get("status") != "SUCCESS":
        v.append(f"agy status={agy.get('status')}")
    denied = agy.get("denied_actions") or []
    if denied:
        v.append("исполнитель без прав: " + ", ".join(str(d.get("display_name", d)) for d in denied))
    if res is None:
        v.append("нет structured_output: ответ не по схеме")
        return v
    st = res.get("status")
    if st not in STATUSES:
        v.append(f"status={st!r} вне {'/'.join(STATUSES)}")
    return v


def validate_role(role, res, changed, writes):
    v, w = [], []
    if res is None or role not in ROLES:
        return v, w
    missing = [k for k in load_schema(role).get("required", []) if k not in res]
    if missing:
        v.append("нет обязательных полей: " + ", ".join(missing))
    if not writes and changed:
        v.append(f"роль {role} без записи, но в дереве изменилось путей: {len(changed)} "
                 f"({', '.join(x[3:] for x in changed[:5])})")
    if role == "code" and res.get("status") == "done" and not res.get("changed_files") and not (res.get("patch") or "").strip():
        w.append("status=done, но changed_files и patch пусты")
    for k, cap in CAPS.items():
        val = res.get(k)
        if isinstance(val, str) and len(val) > cap:
            w.append(f"{k}: {len(val)} символов при лимите {cap}")
        if isinstance(val, list) and len(val) > cap:
            w.append(f"{k}: {len(val)} записей при лимите {cap}")
    conf = res.get("confidence")
    if isinstance(conf, (int, float)) and not 0 <= conf <= 1:
        w.append(f"confidence={conf} вне 0..1")
    return v, w


# ---------- компактный вид ----------

def render_verdicts(items, label):
    counts = {}
    for it in items:
        counts[it.get("verdict", "?")] = counts.get(it.get("verdict", "?"), 0) + 1
    out = [f"{label} ({len(items)}): " + ", ".join(f"{k} {n}" for k, n in counts.items())]
    for it in items:
        if it.get("verdict") != "verified":
            line = f"  [{it.get('verdict')}] {cut(it.get('claim'), 100)} @ {cut(it.get('where'), 60)}"
            if it.get("note"):
                line += f" · {cut(it['note'], 100)}"
            out.append(line)
    return out


def render_explore(res, meta):
    m = res.get("map") or []
    out = [f"map ({len(m)}):"]
    for e in m[:LIST_LIMIT]:
        dep = e.get("depends_on") or []
        out.append(f"  {e.get('path')} · {cut(e.get('purpose'), 90)}" + (f" · зависит от {', '.join(dep[:4])}" if dep else ""))
    if len(m) > LIST_LIMIT:
        out.append(f"  … ещё {len(m) - LIST_LIMIT}, полный список: show --raw")
    return out + render_verdicts(res.get("facts") or [], "facts")


def render_code(res, meta):
    cf = res.get("changed_files") or []
    out = [f"changed_files ({len(cf)}):"]
    out += [f"  {str(c.get('action', '?'))[:3]} {c.get('path')} · {cut(c.get('what'), 90)}" for c in cf[:LIST_LIMIT]]
    if len(cf) > LIST_LIMIT:
        out.append(f"  … ещё {len(cf) - LIST_LIMIT}")
    cm = res.get("commands") or []
    if cm:
        out.append(f"commands ({len(cm)}):")
        out += [f"  [{c.get('exit_code')}] {cut(c.get('cmd'), 60)} · {cut(c.get('summary'), 90)}" for c in cm[:CAPS["commands"]]]
    nd = res.get("not_done") or []
    if nd:
        out.append(f"not_done ({len(nd)}):")
        out += [f"  - {cut(x, 200)}" for x in nd]
    if (res.get("patch") or "").strip():
        out.append(f"patch: {res['patch'].count(chr(10)) + 1} строк, файл {meta.get('patch') or '?'}")
    return out


def render_verify(res, meta):
    g = res.get("gate") or {}
    out = [f"gate: [{g.get('exit_code')}] {cut(g.get('command'), 80)} · passed {g.get('passed')}, "
           f"failed {g.get('failed')}, skipped {g.get('skipped')}"]
    fl = res.get("failures") or []
    if fl:
        out.append(f"failures ({len(fl)}):")
        for f in fl[:LIST_LIMIT]:
            out.append(f"  {cut(f.get('test'), 60)} @ {cut(f.get('where'), 60)}: {cut(f.get('reason'), 120)}")
            if f.get("suspected_cause"):
                out.append(f"      причина: {cut(f['suspected_cause'], 120)}")
    w = res.get("warnings") or []
    if w:
        out.append(f"warnings ({len(w)}): " + "; ".join(cut(x, 80) for x in w[:5]))
    return out


def render_review(res, meta):
    out = render_verdicts(res.get("verdicts") or [], "verdicts")
    rk = res.get("risks") or []
    if rk:
        out.append(f"risks ({len(rk)}):")
        out += [f"  - {cut(r.get('risk'), 110)} @ {cut(r.get('where'), 50)}" for r in rk[:CAPS["risks"]]]
    rec = res.get("recommendation") or {}
    if rec:
        out.append(f"recommendation: {cut(rec.get('choice'), 120)} · {cut(rec.get('reasoning'), 300)}")
    return out


ROLE_RENDER = {"explore": render_explore, "code": render_code, "verify": render_verify, "review": render_review}


def render(role, agy, res, meta, violations, warnings):
    u = agy.get("usage") or {}
    head = f"=== {role} | {res.get('status') if res else '—'}"
    conf = res.get("confidence") if res else None
    if isinstance(conf, (int, float)):
        head += f" | conf {conf:.2f}"
    head += (f" | {meta.get('model', '?')} | {agy.get('num_turns', '?')} turns | "
             f"{agy.get('duration_seconds', 0) or 0:.0f} s | in {fmt_tokens(u.get('input_tokens'))} "
             f"out {fmt_tokens(u.get('output_tokens'))}")
    out = [head, f"conversation: {agy.get('conversation_id', '—')}"]
    if res:
        out.append("summary: " + cut(res.get("summary"), CAPS["summary"]))
        out += ROLE_RENDER.get(role, lambda r, m: [])(res, meta)
        ev = res.get("evidence") or []
        if ev:
            out.append(f"evidence ({len(ev)}): " + "; ".join(f"{e.get('id', '?')} {cut(e.get('where'), 60)}" for e in ev[:CAPS["evidence"]]))
        oq = res.get("open_questions") or []
        if oq:
            out.append(f"open_questions ({len(oq)}):")
            out += [f"  - {cut(q, 200)}" for q in oq]
        if res.get("next_action"):
            out.append("next_action: " + cut(res["next_action"], 300))
    if warnings:
        out.append("contract warnings: " + "; ".join(warnings))
    out.append("contract: OK" if not violations else "CONTRACT VIOLATED: " + "; ".join(violations))
    return "\n".join(out)


# ---------- команды ----------

def append_log(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_patch(res, log, conv):
    p = (res or {}).get("patch") or ""
    if not p.strip():
        return None
    out = log.parent / ".agy-patches" / f"{(conv or 'noconv')[:8]}.diff"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(p if p.endswith("\n") else p + "\n", encoding="utf-8")
    return out


def cmd_run(a):
    brief = pathlib.Path(a.brief).read_text(encoding="utf-8")
    schema_path = SCHEMAS / f"{a.role}.json"
    if not schema_path.exists():
        sys.exit(f"нет схемы: {schema_path}")
    d = pathlib.Path(a.dir).resolve()
    if not d.is_dir():
        sys.exit(f"нет каталога: {d}")
    model = a.model or ROLES[a.role]["model"]
    log = pathlib.Path(a.log).resolve() if a.log else d / ".agy-runs.jsonl"
    writes = ROLES[a.role]["writes"] and a.mode != "plan"
    pre = brief_checks(a.role, brief, a.mode)
    cmd = ["agy", "--dangerously-skip-permissions", "-p", brief, "--add-dir", str(d), "--model", model,
           "--output-format", "json", "--json-schema", str(schema_path), "--print-timeout", a.timeout]
    if a.conversation:
        cmd += ["--conversation", a.conversation]
    if a.effort:
        cmd += ["--effort", a.effort]
    if a.mode:
        cmd += ["--mode", a.mode]
    if a.dry_run:
        print(" ".join(f"'<бриф {len(brief)} символов>'" if x is brief else shlex.quote(x) for x in cmd))
        for w in pre:
            print("warning:", w)
        return 0
    before = git_status(d)
    if before is not None and not git_ignored(d, log):
        pre.append(f"журнал {log.name} не в .gitignore")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=parse_duration(a.timeout) + 60)
        stdout, stderr, timed_out = proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        timed_out = True
    elapsed = time.time() - t0
    after = git_status(d)
    changed = sorted((after or set()) - (before or set())) if before is not None and after is not None else []
    agy, noise = parse_agy_output(stdout)
    res = structured(agy) if agy else None
    meta = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "task": a.task or pathlib.Path(a.brief).stem, "role": a.role, "attempt": a.attempt,
            "model": model, "mode": a.mode, "dir": str(d), "brief_sha": hashlib.sha256(brief.encode()).hexdigest()[:12],
            "brief_chars": len(brief), "wall_seconds": round(elapsed, 1), "worktree_changed": changed}
    tail = []
    if agy is None:
        agy = {"status": "TIMEOUT" if timed_out else "NO_OUTPUT"}
        violations, cw = [f"agy не вернул JSON ({agy['status']})"], []
        tail = (noise + stderr.splitlines())[-15:]
    else:
        violations = validate_core(agy, res)
        rv, cw = validate_role(a.role, res, changed, writes)
        violations += rv
    patch_path = save_patch(res, log, agy.get("conversation_id"))
    warnings = pre + cw
    meta.update(contract="ok" if not violations else "violated", violations=violations,
                contract_warnings=warnings, patch=str(patch_path) if patch_path else None)
    append_log(log, {**agy, "_meta": meta})
    print(render(a.role, agy, res, meta, violations, warnings))
    if tail:
        print("хвост вывода:")
        print("\n".join("  " + cut(x, 160) for x in tail))
    if changed:
        print(f"worktree: изменилось путей {len(changed)} — проверять git diff, не ответ")
    print(f"log: {log}")
    return 0 if not violations else 2


def cmd_show(a):
    log = pathlib.Path(a.log)
    if not log.exists():
        sys.exit(f"журнал не найден: {log}")
    recs = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not recs:
        sys.exit("журнал пуст")
    rec = None
    for r in recs:
        if not a.conversation or str(r.get("conversation_id", "")).startswith(a.conversation):
            rec = r
    if rec is None:
        sys.exit("нет записи с таким conversation_id")
    meta = rec.get("_meta") or {}
    role = meta.get("role") or "unknown"
    res = structured(rec)
    if a.raw:
        print(json.dumps(res if res is not None else rec, ensure_ascii=False, indent=2))
        return 0
    if a.evidence:
        for e in (res or {}).get("evidence") or []:
            if str(e.get("id")) == a.evidence:
                print(f"{e.get('id')} @ {e.get('where')}\n{e.get('excerpt', '')}")
                return 0
        sys.exit(f"нет доказательства с id {a.evidence}")
    violations = meta.get("violations")
    if violations is None:
        violations = validate_core(rec, res) + validate_role(role, res, [], True)[0]
    print(render(role, rec, res, meta, violations, meta.get("contract_warnings") or []))
    return 0 if not violations else 2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="запустить исполнителя по роли")
    r.add_argument("--role", required=True, choices=sorted(ROLES))
    r.add_argument("--dir", required=True, help="рабочий каталог исполнителя, абсолютный путь")
    r.add_argument("--brief", required=True, help="файл с брифом")
    r.add_argument("--model", help="по умолчанию — модель роли")
    r.add_argument("--effort", choices=["low", "medium", "high"])
    r.add_argument("--conversation", help="продолжить сессию исполнителя (доработка)")
    r.add_argument("--mode", choices=["plan"], help="plan: патч текстом в поле patch, дерево не трогать")
    r.add_argument("--timeout", default="20m", help="таймаут agy, по умолчанию 20m")
    r.add_argument("--log", help="журнал прогонов; по умолчанию <dir>/.agy-runs.jsonl")
    r.add_argument("--task", help="метка задачи в журнале; по умолчанию имя файла брифа")
    r.add_argument("--attempt", type=int, default=1)
    r.add_argument("--dry-run", action="store_true", help="показать команду и не запускать")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("show", help="перерисовать результат из журнала")
    s.add_argument("--log", default=".agy-runs.jsonl")
    s.add_argument("--conversation", help="conversation_id или его префикс; по умолчанию последняя запись")
    s.add_argument("--evidence", help="показать одно доказательство целиком по id")
    s.add_argument("--raw", action="store_true", help="сырой structured_output целиком")
    s.set_defaults(fn=cmd_show)
    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
