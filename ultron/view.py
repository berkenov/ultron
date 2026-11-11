"""Context firewall: renders the task result compactly for Claude."""

from __future__ import annotations

from typing import Sequence

from ultron import policy


def cut(s: str | None, n: int) -> str:
    """Collapses whitespace and truncates string to n characters."""
    if s is None:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render_verdicts(items: list[dict], label: str) -> list[str]:
    """Renders a list of verdicts (facts or review verdicts)."""
    counts: dict[str, int] = {}
    for it in items:
        v = it.get("verdict", "?")
        counts[v] = counts.get(v, 0) + 1
    out = [f"{label} ({len(items)}): " + ", ".join(f"{k} {n}" for k, n in counts.items())]
    for it in items:
        if it.get("verdict") != "verified":
            line = f"  [{it.get('verdict')}] {cut(it.get('claim'), 100)} @ {cut(it.get('where'), 60)}"
            if it.get("note"):
                line += f" · {cut(it['note'], 100)}"
            out.append(line)
    return out


def render_explore(res: dict) -> list[str]:
    """Renders explore role sections."""
    m = res.get("map") or []
    out = [f"map ({len(m)}):"]
    for e in m:
        dep = e.get("depends_on") or []
        out.append(f"  {e.get('path')} · {cut(e.get('purpose'), 90)}" + (f" · зависит от {', '.join(dep[:4])}" if dep else ""))
    return out + render_verdicts(res.get("facts") or [], "facts")


def render_code(res: dict) -> list[str]:
    """Renders code role sections."""
    cf = res.get("changed_files") or []
    out = [f"changed_files ({len(cf)}):"]
    for c in cf:
        out.append(f"  {str(c.get('action', '?'))[:3]} {c.get('path')} · {cut(c.get('what'), 90)}")
    
    cm = res.get("commands") or []
    if cm:
        out.append(f"commands ({len(cm)}):")
        for c in cm:
            out.append(f"  [{c.get('exit_code')}] {cut(c.get('cmd'), 60)} · {cut(c.get('summary'), 90)}")
            
    nd = res.get("not_done") or []
    if nd:
        out.append(f"not_done ({len(nd)}):")
        for x in nd:
            out.append(f"  - {cut(x, 200)}")
            
    if (res.get("patch") or "").strip():
        out.append(f"patch: {res['patch'].count(chr(10)) + 1} строк")
    return out


def render_verify(res: dict) -> list[str]:
    """Renders verify role sections."""
    g = res.get("gate") or {}
    out = [f"gate: [{g.get('exit_code')}] {cut(g.get('command'), 80)} · passed {g.get('passed')}, failed {g.get('failed')}, skipped {g.get('skipped')}"]
    fl = res.get("failures") or []
    if fl:
        out.append(f"failures ({len(fl)}):")
        for f in fl:
            out.append(f"  {cut(f.get('test'), 60)} @ {cut(f.get('where'), 60)}: {cut(f.get('reason'), 120)}")
            if f.get("suspected_cause"):
                out.append(f"      причина: {cut(f['suspected_cause'], 120)}")
    w = res.get("warnings") or []
    if w:
        out.append(f"warnings ({len(w)}): " + "; ".join(cut(x, 80) for x in w))
    return out


def render_review(res: dict) -> list[str]:
    """Renders review role sections."""
    out = render_verdicts(res.get("verdicts") or [], "verdicts")
    rk = res.get("risks") or []
    if rk:
        out.append(f"risks ({len(rk)}):")
        for r in rk:
            out.append(f"  - {cut(r.get('risk'), 110)} @ {cut(r.get('where'), 50)}")
            
    rec = res.get("recommendation") or {}
    if rec:
        out.append(f"recommendation: {cut(rec.get('choice'), 120)} · {cut(rec.get('reasoning'), 300)}")
    return out


ROLE_RENDER = {
    "explore": render_explore,
    "code": render_code,
    "verify": render_verify,
    "review": render_review,
}


def compact(result: dict, max_lines: int = policy.COMPACT_MAX_LINES) -> str:
    """Formats TaskResult into a compact string representation."""
    out = []
    
    id_ = result.get("id", "?")
    role = result.get("role", "?")
    st = result.get("status", "?")
    att = result.get("attempts", 0)
    tier = result.get("tier", "?")
    u = result.get("usage") or {}
    sec = round(u.get("seconds") or 0)
    
    def fmt(n: int | float) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
    
    i = fmt(u.get("executor_in", 0))
    c = fmt(u.get("cache_read", 0))
    o = fmt(u.get("executor_out", 0))
    out.append(f"=== {id_} | {role} | {st} | attempts {att} | tier {tier} | {sec} s | in {i} cache {c} out {o}")

    out.append(f"summary: {result.get('summary', '')}")

    rf = result.get("role_fields") or {}
    if role in ROLE_RENDER:
        out.extend(ROLE_RENDER[role](rf))
        
    checks = result.get("checks") or {}
    contract_s = checks.get("contract", "?")
    scope_s = checks.get("scope", "?")
    gate_info = checks.get("gate")
    anchors_s = checks.get("anchors", "?")
    if gate_info:
        gate_s = f"gate [{gate_info.get('exit', '?')}] attempt {gate_info.get('attempt', '?')}"
    else:
        gate_s = "gate —"
    out.append(f"checks: contract {contract_s} · scope {scope_s} · {gate_s} · anchors {anchors_s}")

    restored_list = checks.get("restored") or []
    if restored_list:
        out.append(f"restored: {', '.join(restored_list)}")

    cf = result.get("changed_files") or []
    cl = result.get("claimed_files") or []
    
    if cf:
        out.append(f"changed_files: {', '.join(cf)}")
    else:
        out.append("changed_files: none")
        
    if set(cf) != set(cl):
        if cl:
            out.append(f"claimed: {', '.join(cl)}")
        else:
            out.append("claimed: none")

    ev = result.get("evidence") or []
    if ev:
        out.append(f"evidence ({len(ev)}):")
        for e in ev:
            out.append(f"  {e.get('id', '?')} {e.get('where', '?')}")
            
    oq = result.get("open_questions") or []
    if oq:
        out.append(f"open_questions ({len(oq)}):")
        for q in oq:
            out.append(f"  - {q}")
            
    na = result.get("next_action")
    if na:
        out.append(f"next_action: {na}")
        
    if st != "accepted":
        checks = result.get("checks") or {}
        v = checks.get("violations") or []
        if v:
            out.append(f"violations ({len(v)}):")
            for viol in v:
                out.append(f"  - {viol}")

        failure = result.get("failure") or {}
        if failure:
            out.append(f"failure: {failure.get('domain', '?')}/{failure.get('kind', '?')}")

    handoff = result.get("handoff")
    if handoff:
        out.append(f"handoff: {handoff.get('reason')} — доделай сам" + (f" · сделанное: {handoff['patch']}" if handoff.get("patch") else " · исполнитель ничего не изменил"))

    refs = result.get("refs") or {}
    
    integration = refs.get("integration")
    if integration is not None:
        integration_line = f"integration: {integration}"
        if integration == "conflict" and refs.get("patch"):
            integration_line += f" · патч: {refs.get('patch')}"
        out.append(integration_line)
        
    worktree = refs.get("worktree")
    if worktree is not None:
        out.append(f"worktree: {worktree}")
        
    out.append(f"state: {refs.get('state', '?')}")

    for idx, line in enumerate(out):
        if line.lstrip().startswith("{"):
            out[idx] = " " + line.lstrip()

    orig_len = len(out)
    if orig_len > max_lines:
        out = out[:max_lines - 1]
        out.append(f"… ещё {orig_len - (max_lines - 1)} строк: ultron show {id_} --raw")

    return "\n".join(out)


def evidence_text(attempts: Sequence[dict], evidence_id: str) -> str | None:
    """Extracts the full text of an evidence snippet by ID from the last output."""
    for att in reversed(attempts):
        so = att.get("structured_output")
        if isinstance(so, dict):
            ev = so.get("evidence") or []
            for e in ev:
                if e.get("id") == evidence_id:
                    return f"{evidence_id} @ {e.get('where', '?')}\n{e.get('excerpt', '')}"
    return None
