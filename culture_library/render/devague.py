"""Render a devague frame or plan (schema 1-6) to an HTML fragment.

Pure functions: the only input is the already-parsed dict, the only output is a
string. Every piece of input text is passed through :func:`html.escape`. The
renderer is deliberately tolerant: missing keys render as absent, unknown keys
are ignored, and a valid JSON value of a known schema version never raises.
A schema newer than :data:`MAX_SCHEMA` (or an unrecognised shape) falls back to
a raw, escaped JSON view with a notice paragraph.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Iterable

MAX_SCHEMA = 6
_DELIVERY_KEYS = ("deviations", "evidence", "deltas")


def _e(value: Any) -> str:
    """Escape any value as text (None renders as an empty string)."""
    return "" if value is None else escape(str(value))


def _dicts(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _badge(label: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return f'<span class="dv-{label}">{_e(value)}</span>'


def _list(css: str, items: Iterable[str], tag: str = "ul") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    body = "".join(f"<li>{i}</li>" for i in items)
    return f'<{tag} class="{css}">{body}</{tag}>'


def _section(css: str, heading: str, body: str) -> str:
    if not body:
        return ""
    return f'<section class="dv-{css}"><h3>{_e(heading)}</h3>{body}</section>'


def _para(css: str, label: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    return f'<p class="dv-{css}"><strong>{_e(label)}:</strong> {_e(value)}</p>'


def raw_view(data: Any, notice: str) -> str:
    """Escaped JSON in a ``<pre>`` with a notice paragraph."""
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(data)
    return (
        '<div class="dv-raw">'
        f'<p class="dv-notice">{_e(notice)}</p>'
        f"<pre>{_e(text)}</pre>"
        "</div>"
    )


def detect_kind(data: Any) -> str | None:
    """Return ``"plan"``, ``"frame"``, ``"delivery"`` or ``None`` for an unrecognised dict."""
    if not isinstance(data, dict):
        return None
    if "tasks" in data or "targets" in data:
        return "plan"
    if "claims" in data:
        return "frame"
    if "plan_slug" in data and any(k in data for k in _DELIVERY_KEYS):
        return "delivery"
    return None


def _header(data: dict, kind: str) -> str:
    title = data.get("title") or data.get("slug") or data.get("plan_slug") or kind
    meta = [
        _badge("kind", kind),
        _badge("status", data.get("status")),
        _badge("slug", data.get("slug") or data.get("plan_slug")),
        _badge("schema", f"schema {data['schema_version']}" if "schema_version" in data else None),
        _badge("created", data.get("created")),
        _badge("updated", data.get("updated")),
    ]
    frame_slug = data.get("frame_slug")
    ref = _para("frame-slug", "Frame", frame_slug)
    badges = " ".join(m for m in meta if m)
    return f'<header><h2>{_e(title)}</h2><p class="dv-meta">{badges}</p>{ref}</header>'


def _honesty(conds: Any) -> str:
    items = []
    for h in _dicts(conds):
        items.append(
            f'<span class="dv-id">{_e(h.get("id"))}</span> {_badge("status", h.get("status"))} '
            f'{_e(h.get("text"))}' + (_para("instruction", "Instruction", h.get("instruction")))
        )
    return _section("honesty", "Honesty conditions", _list("dv-honesty-list", items))


def _questions(qs: Any) -> str:
    items = []
    for q in _dicts(qs):
        flags = []
        if q.get("blocking"):
            flags.append(_badge("flag", "blocking"))
        flags.append(_badge("flag", "resolved" if q.get("resolved") else "open"))
        items.append(
            f'<span class="dv-id">{_e(q.get("id"))}</span> {" ".join(flags)} {_e(q.get("text"))}'
            + _para("resolution", "Resolution", q.get("resolution"))
        )
    return _section("questions", "Hard questions", _list("dv-question-list", items))


def _claim(c: dict) -> str:
    links = ", ".join(_e(x) for x in _strs(c.get("links")))
    revisions = c.get("revisions")
    rev = f"{len(revisions)} revision(s)" if isinstance(revisions, list) and revisions else ""
    head = (
        f'<span class="dv-id">{_e(c.get("id"))}</span> '
        f'{_badge("kind", c.get("kind"))} {_badge("status", c.get("status"))} '
        f'{_badge("origin", c.get("origin"))}'
    )
    return (
        '<li class="dv-claim">'
        f"<div>{head}</div>"
        f'<p class="dv-text">{_e(c.get("text"))}</p>'
        + _para("instruction", "Instruction", c.get("instruction"))
        + (f'<p class="dv-links">Links: {links}</p>' if links else "")
        + (f'<p class="dv-revisions">{_e(rev)}</p>' if rev else "")
        + _honesty(c.get("honesty_conditions"))
        + _questions(c.get("hard_questions"))
        + "</li>"
    )


def _claims(data: dict) -> str:
    claims = _dicts(data.get("claims"))
    if not claims:
        return ""
    return _section(
        "claims", "Claims", '<ul class="dv-claim-list">' + "".join(map(_claim, claims)) + "</ul>"
    )


def _scope(data: dict) -> str:
    items = []
    for s in _dicts(data.get("scope_entries")):
        seeds = ", ".join(_e(x) for x in _strs(s.get("seeds")))
        items.append(
            f'<span class="dv-id">{_e(s.get("id"))}</span> <strong>{_e(s.get("surface"))}</strong>'
            f'<p class="dv-finding">{_e(s.get("finding"))}</p>'
            + (f'<p class="dv-seeds">Seeds: {seeds}</p>' if seeds else "")
        )
    return _section("scope", "Scope entries", _list("dv-scope-list", items))


def _vagueness(data: dict) -> str:
    items = []
    for v in _dicts(data.get("open_vagueness")):
        state = "resolved" if v.get("resolved") else "open"
        items.append(
            f'<span class="dv-id">{_e(v.get("id"))}</span> {_badge("kind", v.get("kind"))} '
            f'{_badge("flag", state)} {_e(v.get("text"))}'
            + _para("resolution", "Resolution", v.get("resolution"))
        )
    return _section("vagueness", "Open vagueness", _list("dv-vagueness-list", items))


def _obligations(data: dict) -> str:
    items = []
    for o in _dicts(data.get("obligations")):
        ref = o.get("claim_id") or o.get("task_id")
        items.append(
            f'<span class="dv-id">{_e(o.get("id"))}</span> {_badge("status", o.get("status"))} '
            f'{_badge("ref", ref)} <strong>{_e(o.get("seam"))}</strong>: {_e(o.get("behavior"))}'
        )
    return _section("obligations", "Obligations", _list("dv-obligation-list", items))


def _lapses(data: dict) -> str:
    items = []
    for lp in _dicts(data.get("lapses")):
        items.append(
            f'<span class="dv-id">{_e(lp.get("id"))}</span> {_badge("code", lp.get("code"))} '
            f'{_badge("status", lp.get("status"))} {_e(lp.get("what"))}'
            + _para("skipped", "Skipped check", lp.get("skipped_check"))
        )
    return _section("lapses", "Lapses", _list("dv-lapse-list", items))


def _targets(data: dict) -> str:
    items = []
    for t in _dicts(data.get("targets")):
        deferred = ""
        if t.get("deferred"):
            deferred = _badge("flag", "deferred") + _para(
                "reason", "Reason", t.get("deferred_reason")
            )
        items.append(
            f'<span class="dv-id">{_e(t.get("id"))}</span> {_badge("kind", t.get("kind"))} '
            f'{_e(t.get("text"))}{deferred}'
        )
    return _section("targets", "Coverage targets", _list("dv-target-list", items))


def compute_waves(tasks: list[dict]) -> list[list[dict]]:
    """Group tasks into dependency waves (wave 0 has no dependencies).

    Unknown dependency ids are ignored; tasks caught in a cycle land in a final
    wave rather than raising.
    """
    by_id = {str(t.get("id")): t for t in tasks if t.get("id") is not None}
    deps = {
        tid: {str(d) for d in _strs(t.get("deps")) if str(d) in by_id and str(d) != tid}
        for tid, t in by_id.items()
    }
    waves: list[list[dict]] = []
    placed: set[str] = set()
    while len(placed) < len(by_id):
        ready = [tid for tid in by_id if tid not in placed and deps[tid] <= placed]
        if not ready:
            ready = [tid for tid in by_id if tid not in placed]
        waves.append([by_id[tid] for tid in ready])
        placed.update(ready)
    anonymous = [t for t in tasks if t.get("id") is None]
    if anonymous:
        waves.append(anonymous)
    return waves


def _task(t: dict) -> str:
    criteria = _list("dv-criteria", [_e(c) for c in _strs(t.get("acceptance_criteria"))], "ol")
    deps = ", ".join(_e(x) for x in _strs(t.get("deps")))
    covers = ", ".join(_e(x) for x in _strs(t.get("covers")))
    return (
        '<li class="dv-task">'
        f'<span class="dv-id">{_e(t.get("id"))}</span> {_badge("status", t.get("status"))} '
        f'{_badge("origin", t.get("origin"))} <strong>{_e(t.get("summary"))}</strong>'
        + criteria
        + (f'<p class="dv-deps">Depends on: {deps}</p>' if deps else "")
        + (f'<p class="dv-covers">Covers: {covers}</p>' if covers else "")
        + "</li>"
    )


def _waves(data: dict) -> str:
    tasks = _dicts(data.get("tasks"))
    if not tasks:
        return ""
    parts = []
    for n, wave in enumerate(compute_waves(tasks)):
        parts.append(
            f'<div class="dv-wave"><h4>Wave {n}</h4><ul class="dv-task-list">'
            + "".join(map(_task, wave))
            + "</ul></div>"
        )
    return _section("waves", "Plan waves", "".join(parts))


def _risks(data: dict) -> str:
    items = []
    for r in _dicts(data.get("risks")):
        state = "resolved" if r.get("resolved") else "open"
        items.append(
            f'<span class="dv-id">{_e(r.get("id"))}</span> {_badge("kind", r.get("kind"))} '
            f'{_badge("flag", state)} {_badge("ref", r.get("task_id"))} {_e(r.get("text"))}'
            + _para("resolution", "Resolution", r.get("resolution"))
        )
    return _section("risks", "Risks", _list("dv-risk-list", items))


def _deviations(data: dict) -> str:
    items = []
    for d in _dicts(data.get("deviations")):
        items.append(
            f'<span class="dv-id">{_e(d.get("id"))}</span> {_badge("status", d.get("status"))} '
            f'{_badge("classification", d.get("classification"))} {_e(d.get("what"))}'
            + _para("reason", "Reason", d.get("reason"))
            + _para("task-ref", "Task", d.get("task_ref"))
            + _para("affects", "Affects", ", ".join(_strs(d.get("affects"))))
        )
    return _section("deviations", "Deviations", _list("dv-deviation-list", items))


def _evidence(data: dict) -> str:
    items = []
    for e in _dicts(data.get("evidence")):
        items.append(
            f'<span class="dv-id">{_e(e.get("id"))}</span> '
            f'{_badge("strength", e.get("strength"))} {_badge("outcome", e.get("outcome"))} '
            f'{_badge("status", e.get("status"))} {_badge("type", e.get("evidence_type"))} '
            f'{_e(e.get("behavior_text"))}'
            + _para("obligation-ref", "Obligation", e.get("obligation_ref"))
            + _para("test-ref", "Test", e.get("test_ref"))
            + _para("strength-basis", "Strength basis", e.get("strength_basis"))
        )
    return _section("evidence", "Evidence", _list("dv-evidence-list", items))


def _deltas(data: dict) -> str:
    items = []
    for b in _dicts(data.get("deltas")):
        items.append(
            f'<span class="dv-id">{_e(b.get("id"))}</span> {_badge("kind", b.get("kind"))} '
            f'{_badge("status", b.get("status"))} {_e(b.get("behavior_text"))}'
            + _para("caused-by", "Caused by", ", ".join(_strs(b.get("caused_by"))))
            + _para("evidence-refs", "Evidence", ", ".join(_strs(b.get("evidence_refs"))))
        )
    return _section("deltas", "Behavioral deltas", _list("dv-delta-list", items))


def render(data: Any) -> str:
    """Render a parsed devague frame or plan to an HTML fragment."""
    if not isinstance(data, dict):
        return raw_view(data, "Not a devague frame or plan; showing the raw value.")
    version = data.get("schema_version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        return raw_view(data, f"Unrecognised schema version {version!r}; showing the raw file.")
    if isinstance(version, int) and version > MAX_SCHEMA:
        return raw_view(
            data,
            f"Schema version {version} is newer than the supported {MAX_SCHEMA}; "
            "showing the raw file.",
        )
    kind = detect_kind(data)
    if kind is None:
        return raw_view(
            data,
            "Unrecognised devague shape (no claims, tasks or delivery records); "
            "showing the raw file.",
        )
    if kind == "delivery":
        sections = [_deviations(data), _evidence(data), _deltas(data)]
    elif kind == "plan":
        sections = [_targets(data), _waves(data), _risks(data), _obligations(data)]
    else:
        sections = [
            _claims(data),
            _scope(data),
            _vagueness(data),
            _obligations(data),
            _lapses(data),
        ]
    return (
        f'<article class="devague devague-{kind}" data-schema="{_e(version)}">'
        + _header(data, kind)
        + "".join(sections)
        + "</article>"
    )
