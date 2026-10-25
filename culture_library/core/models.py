"""Plain dataclasses returned by the services.

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/models.py
— cite-don't-import; modified: dropped Device/QueueEntry/ApiKey, widened KINDS, added
visibility and archived_at to Item.
"""

from __future__ import annotations

from dataclasses import dataclass

KINDS = (
    "book",
    "article",
    "newspaper",
    "document",
    "transcript",
    "diary",
    "spec",
    "plan",
    "devague-record",
    "todo",
    "discord-message",
)
VISIBILITIES = ("private", "public")


@dataclass(frozen=True)
class Item:
    id: int
    sha256: str
    kind: str
    title: str
    author: str
    format: str
    size: int
    visibility: str
    created_at: str
    archived_at: str | None = None


@dataclass(frozen=True)
class AddResult:
    item: Item
    created: bool  # False when an identical sha256 already existed
