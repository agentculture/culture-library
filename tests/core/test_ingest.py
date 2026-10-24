"""Ingest tests. Format/cap/zip-bomb/timeout cases adapted from agentculture/xteink @ ea2f643,
path tests/core/test_ingest.py (cite-don't-import); audio refusal, JSON, text extraction and
refusal-recording cases are new. Fixtures are built in-test (no binary blobs)."""

import io
import json
import shutil
import subprocess
import zipfile

import pytest

from culture_library.core import ValidationError
from culture_library.core import ingest as ingest_mod
from culture_library.core.ingest import (
    DEFAULT_LIMITS,
    SUPPORTED_FORMATS,
    IngestLimits,
    convert_markup_to_epub,
    extract_text,
    ingest,
    read_refusals,
)

needs_pdftotext = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="no pdftotext")


def make_epub(chapters=None, extra=None):
    chapters = chapters or {
        "c1.xhtml": "<html><body><h1>Title</h1><p>Hello <b>world</b></p>"
        "<script>evil()</script><style>p{}</style></body></html>"
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, body in chapters.items():
            zf.writestr(f"OEBPS/{name}", body)
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


def make_pdf(text="Hello PDF world"):
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objs[3] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@pytest.mark.parametrize(
    "name,data,fmt",
    [
        ("book.epub", "epub", "epub"),
        ("doc.pdf", "pdf", "pdf"),
        ("note.md", b"# Hi\n\nsome *text*\n", "md"),
        ("note.markdown", b"# Hi\n", "md"),
        ("note.txt", "plain é text".encode(), "txt"),
        ("data.json", b'{"a": [1, 2]}', "json"),
    ],
)
def test_supported_formats_store_original_bytes(store, name, data, fmt):
    data = {"epub": make_epub, "pdf": make_pdf}.get(data, lambda: data)()
    item, created, text = ingest(store, data, name, kind="document")
    assert created is True and item.format == fmt
    assert store.read_bytes(item.id) == data  # byte-identical round trip
    assert isinstance(text, str)
    assert fmt in SUPPORTED_FORMATS


def test_dedup_reports_not_created(store):
    a, c1, _ = ingest(store, b"same", "a.txt", kind="document")
    b, c2, _ = ingest(store, b"same", "b.txt", kind="document")
    assert (c1, c2) == (True, False) and a.id == b.id


def test_title_defaults_to_filename_stem(store):
    item, _, _ = ingest(store, b"x", "My Notes.txt", kind="document")
    assert item.title == "My Notes"
    item2, _, _ = ingest(store, b"y", "z.txt", kind="document", title=" T ", author="A")
    assert (item2.title, item2.author) == ("T", "A")


@pytest.mark.parametrize(
    "name,data",
    [
        ("song.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00abc"),
        ("song.wav", b"RIFF\x00\x00\x00\x00WAVEfmt "),
        ("song.ogg", b"OggS\x00\x02"),
        ("song.flac", b"fLaC\x00\x00"),
        ("song.m4a", b"\x00\x00\x00\x20ftypM4A "),
        ("renamed.txt", b"ID3\x04\x00\x00\x00\x00\x00\x00abc"),  # audio magic under a text name
    ],
)
def test_audio_refused_naming_supported_formats(store, name, data):
    with pytest.raises(ValidationError) as ei:
        ingest(store, data, name, kind="document")
    assert ei.value.code == "unsupported_format"
    for fmt in ("EPUB", "PDF", "Markdown", "text", "JSON"):
        assert fmt in ei.value.detail
    assert "audio" in ei.value.detail.lower()


def test_unknown_extension_refused(store):
    with pytest.raises(ValidationError) as ei:
        ingest(store, b"MZ", "x.exe", kind="document")
    assert ei.value.code == "unsupported_format"


def test_bad_content_refused(store):
    cases = [
        (b"\xff\xfe\x00bad", "a.txt", "invalid_encoding"),
        (b"\xff\xfe", "a.md", "invalid_encoding"),
        (b"\xff", "a.json", "invalid_encoding"),
        (b"{not json", "a.json", "invalid_json"),
        (b"not a pdf", "a.pdf", "bad_magic"),
        (b"not an epub", "a.epub", "bad_magic"),
        (b"", "a.txt", "empty"),
    ]
    for data, name, code in cases:
        with pytest.raises(ValidationError) as ei:
            ingest(store, data, name, kind="document")
        assert ei.value.code == code, name


def test_size_cap(store):
    limits = IngestLimits(max_bytes=10)
    assert DEFAULT_LIMITS.max_bytes == 50 * 1024 * 1024
    with pytest.raises(ValidationError) as ei:
        ingest(store, b"x" * 11, "a.txt", kind="document", limits=limits)
    assert ei.value.code == "too_large"
    ingest(store, b"x" * 10, "a.txt", kind="document", limits=limits)


def test_default_limits_match_xteink():
    assert DEFAULT_LIMITS.max_zip_entries == 5000
    assert DEFAULT_LIMITS.max_uncompressed_bytes == 500 * 1024 * 1024
    assert DEFAULT_LIMITS.convert_timeout_s == 60.0


def test_zip_bomb_refused_and_recorded(store):
    bomb = make_epub(extra={"big.bin": b"\0" * (2 * 1024 * 1024)})
    assert len(bomb) < 20_000  # highly compressible
    limits = IngestLimits(max_uncompressed_bytes=1024 * 1024)
    with pytest.raises(ValidationError) as ei:
        ingest(store, bomb, "bomb.epub", kind="book", limits=limits)
    assert ei.value.code == "zip_bomb"
    records = read_refusals(store)
    assert len(records) == 1
    rec = records[0]
    assert rec["code"] == "zip_bomb" and rec["filename"] == "bomb.epub"
    assert rec["size"] == len(bomb) and rec["at"]
    assert not store.files_dir.exists() or not any(p.is_file() for p in store.files_dir.rglob("*"))


def test_zip_member_count_cap(store):
    many = make_epub(extra={f"m{i}.txt": "x" for i in range(20)})
    with pytest.raises(ValidationError) as ei:
        ingest(store, many, "many.epub", kind="book", limits=IngestLimits(max_zip_entries=10))
    assert ei.value.code == "zip_bomb"


def test_zip_lying_header_caught_while_reading(store):
    """A member whose declared size is small but inflates past the byte cap is refused."""
    data = make_epub(chapters={"c1.xhtml": "<p>" + "a" * 5000 + "</p>"})
    with pytest.raises(ValidationError) as ei:
        extract_text(data, "epub", IngestLimits(max_uncompressed_bytes=1000))
    assert ei.value.code == "zip_bomb"


def test_refusals_recorded_for_every_refusal(store):
    with pytest.raises(ValidationError):
        ingest(store, b"ID3....", "a.mp3", kind="document")
    with pytest.raises(ValidationError):
        ingest(store, b"x" * 20, "a.txt", kind="document", limits=IngestLimits(max_bytes=5))
    assert [r["code"] for r in read_refusals(store)] == ["unsupported_format", "too_large"]


def test_read_refusals_empty(store):
    assert read_refusals(store) == []


def test_epub_text_is_plain(store):
    text = extract_text(make_epub(), "epub")
    assert "Title" in text and "Hello world" in text.replace("\n", " ")
    for bad in ("<", ">", "evil()", "p{}"):
        assert bad not in text


def test_epub_text_skips_non_content_and_orders(store):
    epub = make_epub(
        chapters={"a.xhtml": "<p>first &amp; foremost</p>", "b.html": "<p>second</p>"},
        extra={"OEBPS/style.css": "body{color:red}", "OEBPS/img.png": b"\x89PNG"},
    )
    text = extract_text(epub, "epub")
    assert "first & foremost" in text and "second" in text and "color" not in text


def test_markdown_text_is_plain():
    md = (
        b"# Heading\n\nSome *emph* and **bold** and `code` and [a link](http://x.test/y).\n\n"
        b"- item one\n- item two\n\n```\nfenced\n```\n\n![alt text](img.png)\n<b>raw</b>\n"
    )
    text = extract_text(md, "md")
    for good in ("Heading", "emph", "bold", "code", "a link", "item one", "fenced", "alt text"):
        assert good in text
    for bad in ("#", "*", "`", "](", "http://x.test", "<b>", "- item"):
        assert bad not in text


@needs_pdftotext
def test_pdf_text_extraction(store):
    item, _, text = ingest(store, make_pdf("Searchable words"), "d.pdf", kind="document")
    assert "Searchable words" in text
    assert "%PDF" not in text and "<<" not in text


def test_pdf_without_pdftotext_degrades(monkeypatch, store):
    monkeypatch.setattr(ingest_mod.shutil, "which", lambda name: None)
    item, created, text = ingest(store, make_pdf(), "d.pdf", kind="document")
    assert created and text == ""
    notes = ingest_mod.extract_text_ex(make_pdf(), "pdf")
    assert notes.text == "" and notes.reason == "pdftotext_missing"


def test_pdf_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pdftotext", timeout=kw["timeout"])

    monkeypatch.setattr(ingest_mod.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(ingest_mod.subprocess, "run", boom)
    with pytest.raises(ValidationError) as ei:
        extract_text(make_pdf(), "pdf", IngestLimits(convert_timeout_s=0.5))
    assert ei.value.code == "conversion_timeout"


@needs_pdftotext
def test_pdftotext_invoked_without_shell(monkeypatch):
    seen = {}
    real = subprocess.run

    def spy(argv, **kw):
        seen.update(argv=argv, **kw)
        return real(argv, **kw)

    monkeypatch.setattr(ingest_mod.subprocess, "run", spy)
    extract_text(make_pdf(), "pdf")
    assert isinstance(seen["argv"], list) and seen["shell"] is False
    assert seen["timeout"] == 60.0


def test_json_and_text_extraction():
    assert extract_text(b'{"k": "v"}', "json") == '{"k": "v"}'
    assert extract_text("héllo".encode(), "txt") == "héllo"


def test_convert_markup_optional_pandoc(monkeypatch):
    monkeypatch.setattr(ingest_mod.shutil, "which", lambda name: None)
    assert convert_markup_to_epub(b"# x", "md", title="t") is None


def test_ingest_never_requires_pandoc(monkeypatch, store):
    monkeypatch.setattr(ingest_mod.shutil, "which", lambda name: None)
    item, created, _ = ingest(store, b"# x", "a.md", kind="article")
    assert created and item.format == "md"


def test_json_roundtrip_file_fixture(store, tmp_path):
    payload = json.dumps({"x": 1}).encode()
    item, _, _ = ingest(store, payload, "x.json", kind="document", visibility="public")
    assert item.visibility == "public" and store.read_bytes(item.id) == payload


def test_ingest_threads_on_insert_to_the_store(store):
    calls = []
    item, created, _ = ingest(
        store, b"hello", "h.txt", kind="document", on_insert=lambda c, i: calls.append(i.id)
    )
    assert created and calls == [item.id]
    ingest(store, b"hello", "h.txt", kind="document", on_insert=lambda c, i: calls.append(0))
    assert calls == [item.id]
