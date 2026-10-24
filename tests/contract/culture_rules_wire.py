"""The wire format of culture-rules' ``library.push`` upload, cited verbatim.

Cited (cite-don't-import) from agentculture/culture-rules @ 1edfd8c, path
``culture_rules/node/actions/library.py``: :func:`encode_multipart`, :func:`_header_text`,
:func:`_boundary`, :func:`epub_filename`, :func:`_error_code` and the constants they use,
unmodified. culture-rules wrote that port against xteink feat/issue-1 (ea2f643); the
contract tests replay these exact bytes against culture-library's ``POST /api/library``.
``test_cited_copy_matches_upstream`` fails if a local culture-rules checkout has drifted.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence

MAX_BYTES = 50 * 1024 * 1024
DEFAULT_AUTHOR = "culture-rules"
KIND = "article"
EPUB_TYPE = "application/epub+zip"
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UNSAFE_HEADER_TEXT = re.compile(r'["\r\n]')


def _header_text(text: str) -> str:
    if _UNSAFE_HEADER_TEXT.search(text):
        raise ValueError("multipart names cannot contain quotes or line breaks")
    return text


def encode_multipart(
    fields: Sequence[tuple[str, str]],
    files: Sequence[tuple[str, str, str, bytes]],
) -> tuple[str, bytes]:
    """Encode ``fields`` (name, value) and ``files`` (name, filename, type, bytes).

    Returns ``(content_type, body)``. The boundary is random and checked to be absent from
    every value."""
    parts: list[tuple[str, bytes]] = []
    for name, value in fields:
        head = f'Content-Disposition: form-data; name="{_header_text(name)}"'
        parts.append((head, value.encode("utf-8")))
    for name, filename, content_type, data in files:
        head = (
            f'Content-Disposition: form-data; name="{_header_text(name)}"; '
            f'filename="{_header_text(filename)}"\r\n'
            f"Content-Type: {_header_text(content_type)}"
        )
        parts.append((head, data))
    boundary = _boundary([value for _head, value in parts])
    delimiter = b"--" + boundary.encode("ascii")
    body = bytearray()
    for head, value in parts:
        body += delimiter + b"\r\n" + head.encode("utf-8") + b"\r\n\r\n" + value + b"\r\n"
    body += delimiter + b"--\r\n"
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _boundary(values: Sequence[bytes]) -> str:
    while True:
        boundary = "culture-rules-" + secrets.token_hex(16)
        marker = boundary.encode("ascii")
        if not any(marker in value for value in values):
            return boundary


def epub_filename(title: str) -> str:
    """A safe ``.epub`` filename from ``title`` (``Daily newspaper 2026-10-11`` ->
    ``daily-newspaper-2026-10-11.epub``)."""
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:100].strip("-")
    return f"{slug or 'upload'}.epub"


def _error_code(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _CODE_RE.match(code) else ""
