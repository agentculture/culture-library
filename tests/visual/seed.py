"""The prototype's data, loaded into a library data directory.

Everything the 30 reference screenshots show comes from here: the 17 catalog items (titles,
kinds, sizes and upload times as in the prototype), the 23 Oct newspaper, the Gwen spec and
this library's own devague frame as repo documents, and the two send targets. Items go in
through the library's own ingest path (``library.add``, the code ``POST /api/library`` runs)
and repo documents through ``RepoDocs.push`` (``POST /api/repodocs``); only the timestamps are
then set to the prototype's, because the API stamps "now".
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from culture_library.core import Store, keys, library
from culture_library.core.repodocs import RepoDocs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_STAMP = (2026, 10, 23, 0, 0, 0)

_CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)
_OPF = (
    '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
    'version="3.0" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="uid">urn:fixture:{n}</dc:identifier><dc:title>{title}</dc:title>'
    "<dc:language>en</dc:language></metadata><manifest>"
    '<item id="s01" href="s01.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="s01"/></spine></package>'
)
_CHAPTER = (
    '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>{title}</title></head><body><p>{text}</p></body></html>"
)


def _zip(title: str, n: int, pad: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def put(name: str, data: bytes | str, stored: bool = False) -> None:
            info = zipfile.ZipInfo(name, _STAMP)
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

        put("mimetype", "application/epub+zip", stored=True)
        put("META-INF/container.xml", _CONTAINER)
        put("OEBPS/content.opf", _OPF.format(n=n, title=title))
        # the prototype's card text for an item whose body it did not have
        text = (
            "Daily newspaper from culture-rules. Its full text opens in the reader once the "
            "library indexes EPUB bodies."
        )
        put("OEBPS/s01.xhtml", _CHAPTER.format(title=title, text=text))
        put("OEBPS/pad.bin", b"\0" * pad, stored=True)
    return buf.getvalue()


def padded_epub(title: str, n: int, size: int) -> bytes:
    """A small valid EPUB of exactly ``size`` bytes (a stored, unreferenced member pads it)."""
    base = len(_zip(title, n, 0))
    if size < base:
        raise ValueError(f"{title!r}: {size} bytes is smaller than the {base}-byte minimum")
    data = _zip(title, n, size - base)
    if len(data) != size:
        raise AssertionError((title, len(data), size))
    return data


def seed(data_dir: Path) -> dict:
    """Fill ``data_dir``; return ``{"store", "key", "ids": {"paper", "spec", "frame", ...}}``."""
    catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    store = Store(data_dir)
    ids: dict[str, str] = {}
    for spec in catalog["items"]:
        if "file" in spec:
            data, name = (FIXTURES / spec["file"]).read_bytes(), "paper.epub"
        else:
            data, name = padded_epub(spec["title"], spec["id"], spec["size"]), "item.epub"
        item, created = library.add(
            store,
            data,
            name,
            kind=spec["kind"],
            title=spec["title"],
            author=spec["author"],
            visibility="public",
        )
        assert created and item.size == spec["size"], (spec["title"], item.size)
        with store.connect() as conn:
            conn.execute(
                "UPDATE items SET created_at = ? WHERE id = ?", (spec["created_at"], item.id)
            )
        ids[f"item-{spec['id']}"] = f"item-{item.id}"
    ids["paper"] = ids["item-20"]
    docs = RepoDocs(store)
    for spec in catalog["docs"]:
        pushed = docs.push(
            remote=spec["remote"],
            path=spec["path"],
            commit=spec["commit"],
            committed_at=spec["committed_at"],
            default_branch_head=spec["commit"],
            content=(FIXTURES / spec["file"]).read_text(encoding="utf-8"),
            visibility="public",
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE repodocs SET created_at = ?, updated_at = ? WHERE id = ?",
                (spec["updated_at"], spec["updated_at"], pushed.doc.id),
            )
        ids[spec["key"]] = f"doc-{pushed.doc.id}"
    (Path(data_dir) / "targets.json").write_text(json.dumps(catalog["targets"]), encoding="utf-8")
    _, raw = keys.mint(store, "visual", ["read", "write"], private=True)
    return {"store": store, "key": raw, "ids": ids}
