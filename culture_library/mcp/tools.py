"""Tool specs and handlers. Pure API-client calls; no SDK imports and no library logic.

Provenance: cited (cite-don't-import) from agentculture/xteink @ ea2f643, ``xteink/mcp/tools.py``;
this copy is owned here. Modified: culture-library tools (search, read_item, send to a named
target, diary_*), more kinds and visibility. Every handler runs with the *caller's* client, so the
API enforces that key's scopes and kinds; the diary tools add an ownership check on top.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

from culture_library.core import KINDS, VISIBILITIES
from culture_library.mcp.client import ApiError, Client, ConfigError

__all__ = ["ToolError", "call_tool", "tool_specs"]

ARCHIVE_DAYS = 30
DEFAULT_MAX_BYTES = 256 * 1024
_TEXT_FORMATS = {"md", "txt", "json"}
_KIND = {"type": "string", "enum": list(KINDS)}
_VIS = {"type": "string", "enum": list(VISIBILITIES)}
_ID = {"type": "integer", "minimum": 1}


def _spec(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


_SPECS: list[dict[str, Any]] = [
    _spec(
        "push_file",
        "Add a file to the library (EPUB, PDF, Markdown, text, JSON). Give `content` (text) or "
        "`content_base64` with `filename`; `path` (a file on the MCP host) works over stdio only. "
        "Visibility defaults to private. Identical bytes are not duplicated (`created: false`).",
        {
            "path": {"type": "string", "description": "file path on the MCP host (stdio only)"},
            "content": {"type": "string", "description": "file text (e.g. Markdown)"},
            "content_base64": {"type": "string", "description": "binary file, base64"},
            "filename": {"type": "string", "description": "e.g. article.md"},
            "title": {"type": "string"},
            "author": {"type": "string"},
            "kind": _KIND,
            "visibility": _VIS,
        },
    ),
    _spec(
        "list_library",
        "List library items, or filter title/author with q.",
        {
            "q": {"type": "string"},
            "kind": _KIND,
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "offset": {"type": "integer", "minimum": 0},
        },
    ),
    _spec(
        "search",
        "Search item content (full text, semantic or hybrid).",
        {
            "q": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": ["fulltext", "semantic", "hybrid"]},
            "kind": _KIND,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ["q"],
    ),
    _spec(
        "read_item",
        "Read an item: its metadata plus `text` for Markdown/text/JSON items, otherwise the file "
        "as `content_base64` (omitted, with `truncated: true`, when larger than max_bytes).",
        {"item_id": _ID, "max_bytes": {"type": "integer", "minimum": 1}},
        ["item_id"],
    ),
    _spec(
        "send_to_device",
        "Ask the library to send an item to a named device target (e.g. an e-reader).",
        {"item_id": _ID, "target": {"type": "string"}},
        ["item_id", "target"],
    ),
    _spec(
        "diary_write",
        "Write a new diary entry (a Markdown item of kind diary, private unless told otherwise).",
        {
            "text": {"type": "string", "minLength": 1},
            "title": {"type": "string"},
            "visibility": _VIS,
        },
        ["text"],
    ),
    _spec(
        "diary_edit",
        "Replace one of your own diary entries: the new text is stored as a new entry and the old "
        "one is archived. Refused for entries other keys wrote and for non-diary items.",
        {"entry_id": _ID, "text": {"type": "string", "minLength": 1}},
        ["entry_id", "text"],
    ),
    _spec(
        "diary_delete",
        f"Delete one of your own diary entries (archived, restorable for {ARCHIVE_DAYS} days). "
        "Refused for entries other keys wrote and for non-diary items.",
        {"entry_id": _ID},
        ["entry_id"],
    ),
]


class ToolError(Exception):
    """A failed tool call; the message is shown to the calling model."""


def tool_specs() -> list[dict[str, Any]]:
    return [dict(s) for s in _SPECS]


def _need(a: dict, name: str) -> Any:
    if a.get(name) is None:
        raise ToolError(f"invalid_arguments: {name} is required")
    return a[name]


def _brief(item: dict) -> dict:
    keys = ("id", "title", "author", "kind", "format", "size", "visibility", "created_at")
    return {k: item[k] for k in keys if k in item}


def _inline_bytes(a: dict) -> bytes:
    if not a.get("filename"):
        raise ToolError("invalid_arguments: filename is required with content")
    if a.get("content") is not None:
        return a["content"].encode("utf-8")
    try:
        return base64.b64decode(a["content_base64"], validate=True)
    except ValueError:  # binascii.Error is a ValueError
        raise ToolError("invalid_arguments: content_base64 is not valid base64") from None


def _push_file(client: Client, a: dict) -> dict:
    given = [k for k in ("path", "content", "content_base64") if a.get(k) is not None]
    if len(given) != 1:
        raise ToolError("invalid_arguments: give exactly one of path, content, content_base64")
    kw = {k: a[k] for k in ("title", "kind", "visibility") if a.get(k) is not None}
    kw["author"] = a.get("author") or ""
    if "path" in given:
        try:
            res = client.upload(path=a["path"], filename=a.get("filename") or "", **kw)
        except OSError as exc:
            raise ToolError(f"unreadable_path: {exc.strerror or exc}") from None
    else:
        res = client.upload(_inline_bytes(a), a["filename"], **kw)
    return {"item": _brief(res["item"]), "created": res["created"]}


def _read_item(client: Client, a: dict) -> dict:
    item = client.get_item(_need(a, "item_id"))
    cap = int(a.get("max_bytes") or DEFAULT_MAX_BYTES)
    out: dict[str, Any] = {"item": _brief(item)}
    is_text = item["format"] in _TEXT_FORMATS
    if item["size"] > cap and not is_text:
        out["truncated"] = True
        return out
    data = client.get_file(item["id"])
    if is_text:
        out["text"] = data[:cap].decode("utf-8", "replace")
        out["truncated"] = len(data) > cap
    else:
        out["content_base64"] = base64.b64encode(data).decode("ascii")
        out["truncated"] = False
    return out


# -- diary -------------------------------------------------------------------


def _diary_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "diary entry"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _own_diary_entry(client: Client, entry_id: int) -> dict:
    """The item, if it is a diary entry created by the caller's own key; else ToolError."""
    item = client.get_item(entry_id)  # 404 when absent, archived or private-to-others
    if item["kind"] != "diary":
        raise ToolError(
            f"not_a_diary_entry: item {entry_id} is a {item['kind']}, not a diary entry"
        )
    if not client.uploaded_by_me(entry_id, item):
        raise ToolError(f"not_owner: diary entry {entry_id} was not written by this key")
    return item


def _diary_write(client: Client, a: dict) -> dict:
    text = _need(a, "text")
    if not text.strip():
        raise ToolError("invalid_arguments: text is empty")
    res = client.upload(
        text.encode("utf-8"),
        f"diary-{_stamp()}.md",
        title=a.get("title") or _diary_title(text),
        kind="diary",
        visibility=a.get("visibility") or "private",
    )
    return {"item": _brief(res["item"]), "created": res["created"]}


def _diary_edit(client: Client, a: dict) -> dict:
    entry_id, text = _need(a, "entry_id"), _need(a, "text")
    if not text.strip():
        raise ToolError("invalid_arguments: text is empty")
    old = _own_diary_entry(client, entry_id)
    body = f"{text.rstrip()}\n\n<!-- replaces: {entry_id} -->\n"
    res = client.upload(
        body.encode("utf-8"),
        f"diary-{_stamp()}.md",
        title=_diary_title(text),
        kind="diary",
        visibility=old["visibility"],
    )
    new_id = res["item"]["id"]
    try:
        client.archive(entry_id)
    except ApiError as exc:
        raise ToolError(
            f"partial_edit: new entry {new_id} was stored but entry {entry_id} could not be "
            f"archived: {exc}"
        ) from None
    return {"item": _brief(res["item"]), "replaces": entry_id, "archived": entry_id}


def _diary_delete(client: Client, a: dict) -> dict:
    entry_id = _need(a, "entry_id")
    _own_diary_entry(client, entry_id)
    client.archive(entry_id)
    return {"archived": entry_id, "restorable_days": ARCHIVE_DAYS}


def _handlers() -> dict[str, Any]:
    return {
        "push_file": _push_file,
        "list_library": lambda c, a: {
            "items": [
                _brief(i)
                for i in c.list_library(
                    a.get("q"), a.get("kind"), a.get("limit", 100), a.get("offset", 0)
                )
            ]
        },
        "search": lambda c, a: c.search(
            _need(a, "q"), a.get("mode"), a.get("kind"), a.get("limit", 20)
        ),
        "read_item": _read_item,
        "send_to_device": lambda c, a: c.send(_need(a, "item_id"), _need(a, "target")),
        "diary_write": _diary_write,
        "diary_edit": _diary_edit,
        "diary_delete": _diary_delete,
    }


def call_tool(name: str, arguments: dict[str, Any] | None, client_factory: Any) -> Any:
    handler = _handlers().get(name)
    if handler is None:
        raise ToolError(f"unknown tool {name!r}")
    try:
        return handler(client_factory(), dict(arguments or {}))
    except ApiError as exc:
        raise ToolError(str(exc)) from None
    except ConfigError as exc:
        raise ToolError(f"missing_key: {exc}") from None
    except ValueError as exc:
        raise ToolError(f"invalid_arguments: {exc}") from None
