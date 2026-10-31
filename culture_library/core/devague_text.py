"""Turn devague JSON (frame, plan, delivery) into prose for indexing.

Stdlib-only and network-free. The shape is detected from the keys, the same
way ``render/devague.py`` does (claims -> frame, tasks/targets -> plan,
plan_slug + evidence -> delivery); this module deliberately does not import
``render/``. Every output line is a sentence-like label plus text, never bare
JSON punctuation or a key, so a chunker sees prose.
"""

from __future__ import annotations

from typing import Any, Iterable


def detect_kind(data: Any) -> str | None:
    """Return ``"plan"``, ``"frame"``, ``"delivery"`` or ``None``."""
    if not isinstance(data, dict):
        return None
    if "tasks" in data or "targets" in data:
        return "plan"
    if "claims" in data:
        return "frame"
    if "plan_slug" in data and "evidence" in data:
        return "delivery"
    return None


def _dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    return []


def _s(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _line(label: str, *parts: Any) -> str | None:
    body = " ".join(p for p in (_s(x) for x in parts) if p)
    return f"{label}: {body}" if body else None


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = _s(value)
        if text:
            yield text
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _ident(item: dict) -> str:
    return _s(item.get("id"))


def _frame(data: dict) -> list[str | None]:
    out: list[str | None] = []
    for c in _dicts(data.get("claims")):
        cid = _ident(c)
        out.append(_line(f"Claim {cid} ({_s(c.get('kind'))})".replace(" ()", ""), c.get("text")))
        for h in _dicts(c.get("honesty_conditions")):
            out.append(_line(f"Honesty condition {_ident(h)} on {cid}", h.get("text")))
        for q in _dicts(c.get("hard_questions")):
            out.append(_line(f"Hard question {_ident(q)} on {cid}", q.get("text")))
            out.append(_line(f"Answer to {_ident(q)}", q.get("resolution")))
    for v in _dicts(data.get("open_vagueness")):
        out.append(_line(f"Open question {_ident(v)}", v.get("text")))
        out.append(_line(f"Resolution of {_ident(v)}", v.get("resolution")))
    for s in _dicts(data.get("scope_entries")):
        out.append(_line(f"Scope finding {_ident(s)} ({_s(s.get('surface'))})", s.get("finding")))
    for lp in _dicts(data.get("lapses")):
        out.append(_line(f"Lapse {_ident(lp)}", lp.get("what"), lp.get("skipped_check")))
    for o in _dicts(data.get("obligations")):
        out.append(_line(f"Obligation {_ident(o)}", o.get("behavior")))
    return out


def _plan(data: dict) -> list[str | None]:
    out: list[str | None] = []
    for t in _dicts(data.get("targets")):
        out.append(_line(f"Target {_ident(t)}", t.get("text")))
    for t in _dicts(data.get("tasks")):
        tid = _ident(t)
        out.append(_line(f"Task {tid}", t.get("summary")))
        out.append(_line(f"Instruction for {tid}", t.get("instruction")))
        crit = t.get("acceptance_criteria")
        for a in crit if isinstance(crit, list) else []:
            out.append(_line(f"Acceptance criterion for {tid}", a))
    for r in _dicts(data.get("risks")):
        out.append(_line(f"Risk {_ident(r)}", r.get("text")))
        out.append(_line(f"Resolution of {_ident(r)}", r.get("resolution")))
    for o in _dicts(data.get("obligations")):
        out.append(_line(f"Obligation {_ident(o)}", o.get("behavior")))
    return out


def _delivery(data: dict) -> list[str | None]:
    out: list[str | None] = []
    for d in _dicts(data.get("deviations")):
        out.append(_line(f"Deviation {_ident(d)}", d.get("what"), d.get("reason")))
    for e in _dicts(data.get("evidence")):
        out.append(_line(f"Evidence {_ident(e)}", e.get("behavior_text"), e.get("contract_text")))
        out.append(_line(f"Outcome of {_ident(e)}", e.get("outcome")))
    for b in _dicts(data.get("deltas")):
        out.append(
            _line(f"Behavioral delta {_ident(b)} ({_s(b.get('kind'))})", b.get("behavior_text"))
        )
    return out


def text_for(path: str, data: Any) -> str:
    """Return indexable prose for a parsed devague JSON document.

    ``path`` is only used for the fallback title; unknown shapes yield their
    string values only (no keys, no punctuation-only lines).
    """
    kind = detect_kind(data)
    lines: list[str | None] = []
    if kind is not None:
        title = _s(data.get("title")) or _s(data.get("slug")) or _s(data.get("plan_slug"))
        if title:
            lines.append(title)
        lines.append(f"Devague {kind}" + (f" from {path}" if path else ""))
        lines += {"frame": _frame, "plan": _plan, "delivery": _delivery}[kind](data)
    else:
        lines = list(_strings(data))
    return "\n".join(x for x in lines if x and any(ch.isalnum() for ch in x)) + "\n"
