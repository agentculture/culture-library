"""Upload containment, format checks and plain-text extraction for search.

Cited from agentculture/xteink @ ea2f643 (branch feat/issue-1), path xteink/core/ingest.py
-- cite-don't-import; modified: formats are EPUB, PDF, Markdown, text and JSON (BMP dropped,
audio refused); originals are always stored as uploaded (no Markdown/HTML to EPUB rewrite on
the ingest path -- ``convert_markup_to_epub`` keeps the pandoc conversion as an optional,
skip-when-missing helper); ``IngestError`` replaced by ``ValidationError`` carrying a code;
the library service replaced by :class:`~culture_library.core.store.Store`; added
:func:`extract_text` (stdlib ``zipfile``/``html.parser`` for EPUB, ``pdftotext`` subprocess for
PDF) and a refusal record.

Every upload path (web, API, MCP) must go through :func:`ingest`. Guards, in order: size cap,
format/magic check (audio refused), EPUB zip-bomb caps (member count, declared and actually
inflated bytes), then text extraction. A zip that opens but whose members cannot be read
(bad CRC, corrupt deflate data, unsupported compression, encryption) is refused as
``corrupt_archive``; a file that is not a zip at all stays ``bad_magic``. Nothing is ever
executed; the only subprocess is a ``shell=False`` fixed-argv ``pdftotext`` (and optional
``pandoc``) bounded by a timeout.

Refusals: every ``ValidationError`` raised by :func:`ingest` is (a) logged on the
``culture_library.ingest`` logger at WARNING and (b) appended as one JSON line
``{at, code, detail, filename, size}`` to ``<data_dir>/refusals.jsonl``; read it back with
:func:`read_refusals`. The raised error carries ``.code`` and ``.detail`` for the API's
``{detail, code}`` body.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - shell=False, fixed argv (pdftotext / pandoc)
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .errors import ValidationError
from .models import Item
from .store import Store

log = logging.getLogger("culture_library.ingest")

EPUB_MIMETYPE = b"application/epub+zip"
PDF_MAGIC = b"%PDF"
REFUSALS_FILE = "refusals.jsonl"

SUPPORTED_FORMATS = ("epub", "pdf", "md", "txt", "json")
SUPPORTED_DESCRIPTION = "EPUB, PDF, Markdown, text and JSON"
_EXT_FORMAT = {
    ".epub": "epub",
    ".pdf": "pdf",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
    ".json": "json",
}
_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma", ".aiff"}
_PANDOC_FROM = {"md": "markdown", "html": "html"}


@dataclass(frozen=True)
class IngestLimits:
    max_bytes: int = 60 * 1024 * 1024
    max_zip_entries: int = 5000
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    convert_timeout_s: float = 60.0


DEFAULT_LIMITS = IngestLimits()


@dataclass(frozen=True)
class Extraction:
    """Extracted plain text plus, when text is empty by degradation, why."""

    text: str
    reason: str = ""


def _unsupported(extra: str = "") -> ValidationError:
    return ValidationError(
        f"{extra}Supported formats: {SUPPORTED_DESCRIPTION}. Audio is not supported.",
        "unsupported_format",
    )


def _looks_like_audio(data: bytes) -> bool:
    head = data[:16]
    return (
        head.startswith((b"ID3", b"OggS", b"fLaC", b"FORM"))
        or (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
        or head[4:8] == b"ftyp"
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # MPEG layer III frame sync
    )


def _decode(data: bytes) -> str:
    if b"\x00" in data:
        raise ValidationError("file is not valid UTF-8 text", "invalid_encoding")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("file is not valid UTF-8 text", "invalid_encoding") from exc


# Reading a member of a damaged archive: BadZipFile (bad CRC, truncated header), zlib.error
# (corrupt deflate stream), NotImplementedError (unsupported compression method; a
# RuntimeError subclass, named for clarity), RuntimeError (encrypted member), EOFError
# (truncated stream). All become one refusal code, ``corrupt_archive``.
_ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError)


def _corrupt() -> ValidationError:
    return ValidationError(
        "EPUB archive is corrupt, encrypted or uses an unsupported compression method",
        "corrupt_archive",
    )


def _open_epub(data: bytes, limits: IngestLimits) -> zipfile.ZipFile | None:
    """Return the ZipFile if data is an EPUB within caps; None if not an EPUB; raise on bomb.

    A zip whose ``mimetype`` member cannot be read (bad CRC, encrypted, unsupported
    compression) raises ``ValidationError`` with code ``corrupt_archive``.
    """
    if not data.startswith(b"PK"):
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    infos = zf.infolist()
    if len(infos) > limits.max_zip_entries:
        raise ValidationError("EPUB has too many entries", "zip_bomb")
    if sum(i.file_size for i in infos) > limits.max_uncompressed_bytes:
        raise ValidationError("EPUB uncompressed size is too large", "zip_bomb")
    if not infos or infos[0].filename != "mimetype" or infos[0].file_size > len(EPUB_MIMETYPE):
        return None
    try:
        mimetype = zf.read("mimetype")
    except _ZIP_READ_ERRORS as exc:
        raise _corrupt() from exc
    if mimetype != EPUB_MIMETYPE:
        return None
    return zf


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: int) -> bytes:
    """Read at most ``budget`` actually-inflated bytes (headers can lie); raise past it.

    A damaged member raises ``ValidationError`` with code ``corrupt_archive``.
    """
    try:
        with zf.open(info) as fh:
            out = fh.read(budget + 1)
    except _ZIP_READ_ERRORS as exc:
        raise _corrupt() from exc
    if len(out) > budget:
        raise ValidationError("EPUB inflates beyond the uncompressed size limit", "zip_bomb")
    return out


class _TextParser(HTMLParser):
    _SKIP = {"script", "style", "head", "title"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(markup: str) -> str:
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    return _collapse("".join(parser.parts))


def _epub_text(data: bytes, limits: IngestLimits) -> str:
    zf = _open_epub(data, limits)
    if zf is None:
        raise ValidationError("not a valid EPUB", "bad_magic")
    budget = limits.max_uncompressed_bytes
    chunks: list[str] = []
    with zf:
        names = sorted(
            (i for i in zf.infolist() if i.filename.lower().endswith((".xhtml", ".html", ".htm"))),
            key=lambda i: i.filename,
        )
        for info in names:
            raw = _read_member(zf, info, budget)
            budget -= len(raw)
            chunks.append(_html_to_text(raw.decode("utf-8", errors="replace")))
    return "\n\n".join(c for c in chunks if c)


_MD_RULES = [
    (re.compile(r"```[^\n]*\n(.*?)```", re.S), r"\1"),  # fenced code: keep body
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # image -> alt
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # link -> label
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # headings
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),  # quotes
    (re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M), ""),  # list markers
    (re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M), ""),  # rules
    (re.compile(r"`+"), ""),  # inline code ticks
    (re.compile(r"(\*\*|__|\*|~~)"), ""),  # emphasis
]


def _markdown_text(data: bytes) -> str:
    text = _decode(data)
    for pattern, repl in _MD_RULES:
        text = pattern.sub(repl, text)
    return _html_to_text(text)  # drops raw inline HTML tags and decodes entities


def _pdf_text(data: bytes, limits: IngestLimits) -> Extraction:
    exe = shutil.which("pdftotext")
    if exe is None:
        return Extraction("", "pdftotext_missing")
    # The PDF goes in on stdin (poppler's ``fd://0``): no temp file, no path built from input.
    argv = [exe, "-q", "-enc", "UTF-8", "fd://0", "-"]
    try:
        proc = subprocess.run(  # nosec B603 - shell=False, fixed argv, no network
            argv,
            shell=False,
            input=data,
            capture_output=True,
            timeout=limits.convert_timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("PDF text extraction timed out", "conversion_timeout") from exc
    except OSError:
        return Extraction("", "pdftotext_unavailable")
    if proc.returncode != 0:
        return Extraction("", "pdftotext_failed")
    return Extraction(_collapse(proc.stdout.decode("utf-8", errors="replace").replace("\f", "\n")))


def extract_text_ex(data: bytes, fmt: str, limits: IngestLimits = DEFAULT_LIMITS) -> Extraction:
    """Plain text for search, with a degradation reason when it is empty by design."""
    if fmt == "epub":
        return Extraction(_epub_text(data, limits))
    if fmt == "pdf":
        return _pdf_text(data, limits)
    if fmt == "md":
        return Extraction(_markdown_text(data))
    if fmt in ("txt", "json"):
        return Extraction(_decode(data))
    raise _unsupported()


def extract_text(data: bytes, fmt: str, limits: IngestLimits = DEFAULT_LIMITS) -> str:
    """Plain text (no markup) for ``fmt`` in :data:`SUPPORTED_FORMATS`."""
    return extract_text_ex(data, fmt, limits).text


def convert_markup_to_epub(
    data: bytes,
    fmt: str,
    *,
    title: str,
    author: str = "",
    limits: IngestLimits = DEFAULT_LIMITS,
) -> bytes | None:
    """Optional pandoc Markdown/HTML to EPUB. Returns None when pandoc is not installed."""
    pandoc = shutil.which("pandoc")
    if pandoc is None or fmt not in _PANDOC_FROM:
        return None
    with tempfile.TemporaryDirectory(prefix="culture-library-ingest-") as tmp:
        src, out, meta = Path(tmp) / "input", Path(tmp) / "output.epub", Path(tmp) / "meta.yaml"
        src.write_bytes(data)
        fields = {"title": title, **({"author": author} if author else {})}
        meta.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        argv = [pandoc, "--sandbox", "-f", _PANDOC_FROM[fmt], "-t", "epub",
                "--metadata-file", str(meta), "-o", str(out), str(src)]  # fmt: skip
        try:
            proc = subprocess.run(  # nosec B603 - shell=False, argv list, no URLs
                argv,
                shell=False,
                capture_output=True,
                timeout=limits.convert_timeout_s,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("conversion timed out", "conversion_timeout") from exc
        except OSError:
            return None
        if proc.returncode != 0 or not out.is_file():
            raise ValidationError("conversion failed", "conversion_failed")
        result = out.read_bytes()
    if len(result) > limits.max_bytes or _open_epub(result, limits) is None:
        raise ValidationError("converter produced an invalid EPUB", "conversion_failed")
    return result


def _validate(data: bytes, filename: str, limits: IngestLimits) -> str:
    """Return the stored format for an upload, or raise ValidationError."""
    if len(data) > limits.max_bytes:
        raise ValidationError("file exceeds the size limit", "too_large")
    if not data:
        raise ValidationError("file is empty", "empty")
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _AUDIO_EXTS or (ext not in (".epub", ".pdf") and _looks_like_audio(data)):
        raise _unsupported("Audio files are not accepted. ")
    fmt = _EXT_FORMAT.get(ext)
    if fmt is None:
        raise _unsupported(f"Unsupported file type {ext or '(none)'}. ")
    _check_content(data, fmt, limits)
    return fmt


def _check_content(data: bytes, fmt: str, limits: IngestLimits) -> None:
    """Raise ValidationError when ``data`` is not a well-formed ``fmt`` file."""
    if fmt == "pdf" and not data.startswith(PDF_MAGIC):
        raise ValidationError("not a valid PDF", "bad_magic")
    if fmt == "epub" and _open_epub(data, limits) is None:
        raise ValidationError("not a valid EPUB", "bad_magic")
    if fmt in ("md", "txt"):
        _decode(data)
    if fmt == "json":
        try:
            json.loads(_decode(data))
        except ValueError as exc:
            raise ValidationError("file is not valid JSON", "invalid_json") from exc


def _record_refusal(store: Store, err: ValidationError, filename: str, size: int) -> None:
    rec = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code": err.code,
        "detail": err.detail,
        "filename": os.path.basename(filename or ""),
        "size": size,
    }
    log.warning("ingest refused: %s", json.dumps(rec, sort_keys=True))
    try:
        with open(store.data_dir / REFUSALS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:  # recording must never mask the refusal itself
        log.exception("could not append to %s", REFUSALS_FILE)


def read_refusals(store: Store) -> list[dict]:
    """All recorded refusals, oldest first."""
    path = store.data_dir / REFUSALS_FILE
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def ingest(
    store: Store,
    data: bytes,
    filename: str,
    *,
    kind: str,
    title: str | None = None,
    author: str = "",
    visibility: str | None = None,
    limits: IngestLimits = DEFAULT_LIMITS,
    on_insert: Callable[[Any, Item], None] | None = None,
    on_text: Callable[[str], None] | None = None,
) -> tuple[Item, bool, str]:
    """Validate and store an upload as-is; return ``(item, created, extracted_text)``.

    Raises :class:`ValidationError` (``.code`` in unsupported_format, too_large, empty,
    bad_magic, invalid_encoding, invalid_json, zip_bomb, corrupt_archive, conversion_timeout,
    plus the store's own validation) after recording the refusal. PDF text degrades to ``""``
    when pdftotext is absent. ``on_insert`` is passed to :meth:`Store.add_item` (runs in the
    insert's transaction; not called for a duplicate). ``on_text(text)`` receives the
    extracted text once the upload is accepted and before it is stored (so ``on_insert`` can
    index it without extracting again).
    """
    try:
        fmt = _validate(data, filename, limits)
        text = extract_text_ex(data, fmt, limits).text
    except ValidationError as err:
        _record_refusal(store, err, filename, len(data))
        raise
    if on_text is not None:
        on_text(text)
    stem = os.path.splitext(os.path.basename(filename or ""))[0] or "Untitled"
    result = store.add_item(
        data,
        kind=kind,
        title=(title or "").strip() or stem,
        format=fmt,
        author=author,
        visibility=visibility,
        on_insert=on_insert,
    )
    return result.item, result.created, text


__all__ = [
    "DEFAULT_LIMITS",
    "Extraction",
    "IngestLimits",
    "SUPPORTED_FORMATS",
    "convert_markup_to_epub",
    "extract_text",
    "extract_text_ex",
    "ingest",
    "read_refusals",
]
