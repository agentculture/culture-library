"""OPDS 1.2 catalog and read-only WebDAV (plan task t14, decision c46).

Structure is validated by parsing the Atom / multistatus XML with ``xml.etree`` in the test.
"""

from __future__ import annotations

import base64
import io
import zipfile
from xml.etree import ElementTree as ET  # nosec B405 - parsing our own test output

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from culture_library.core import Store, keys, library  # noqa: E402
from culture_library.server import app as server_app  # noqa: E402
from culture_library.server.auth import Principal, current_principal  # noqa: E402

ATOM = "{http://www.w3.org/2005/Atom}"
DAV = "{DAV:}"
ACQ = "http://opds-spec.org/acquisition"
WRITE_METHODS = ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK", "POST"]


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>Call me Ishmael</p></body></html>")
    return buf.getvalue()


EPUB = make_epub()


class Env:
    def __init__(self, tmp_path) -> None:
        self.app = server_app.create_app()
        self.store = self.app.state.store = Store(tmp_path / "data")
        self.c = TestClient(self.app)
        self.owner = keys.mint(self.store, "o", ["read"], private=True)[1]
        self.reader = keys.mint(self.store, "r", ["read"])[1]
        self.book = self.add(EPUB, "b.epub", "Moby Dick", "Melville", "book", "public")
        self.note = self.add(b"# n", "n.md", "Secret/Note", "me", "diary", "private")
        self.pub_md = self.add(b"# p", "p.md", "Pub Note", "me", "diary", "public")
        self.pdf = self.add(b"%PDF-1.4 x", "d.pdf", "Doc", "", "document", "public")
        gone = self.add(b"# g", "g.md", "Gone", "", "diary", "public")
        library.archive(self.store, gone.id)

    def add(self, data, filename, title, author, kind, visibility):
        return library.add(
            self.store,
            data,
            filename,
            title=title,
            author=author,
            kind=kind,
            visibility=visibility,
        )[0]

    @staticmethod
    def basic(key: str, user: str = "anyone") -> dict:
        tok = base64.b64encode(f"{user}:{key}".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    @staticmethod
    def bearer(key: str) -> dict:
        return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def parse(resp) -> ET.Element:
    return ET.fromstring(resp.content)  # nosec B314 - our own response


# ------------------------------------------------------------------ auth


def test_missing_credentials_challenge_basic(env):
    for path in ("/opds", "/opds/book", "/dav/"):
        r = env.c.request("PROPFIND" if path == "/dav/" else "GET", path)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == 'Basic realm="culture-library"'


def test_basic_and_bearer_both_work_any_username(env):
    assert env.c.get("/opds", headers=env.basic(env.reader, "whoever")).status_code == 200
    assert env.c.get("/opds", headers=env.bearer(env.reader)).status_code == 200
    assert env.c.get("/opds", headers=env.basic("clk_bad")).status_code == 401
    r = env.c.get("/opds", headers={"Authorization": "Basic !!notbase64"})
    assert r.status_code == 401


def no_read(env) -> None:
    # a valid caller whose scopes lack "read" (a minted key's write/admin scopes imply it)
    env.app.dependency_overrides[current_principal] = lambda: Principal("x", frozenset())


def test_read_scope_required(env):
    no_read(env)
    assert env.c.get("/opds", headers=env.basic(env.reader)).status_code == 403


# ------------------------------------------------------------------ OPDS


def test_root_navigation_lists_kinds_with_visible_items(env):
    r = env.c.get("/opds", headers=env.basic(env.reader))
    assert r.headers["content-type"].startswith("application/atom+xml")
    assert "profile=opds-catalog" in r.headers["content-type"]
    assert "kind=navigation" in r.headers["content-type"]
    feed = parse(r)
    assert feed.tag == ATOM + "feed"
    for tag in ("id", "title", "updated"):
        assert feed.find(ATOM + tag) is not None
    rels = {link.get("rel"): link for link in feed.findall(ATOM + "link")}
    assert {"self", "start", "search"} <= set(rels)
    hrefs = {e.find(ATOM + "link").get("href") for e in feed.findall(ATOM + "entry")}
    # reader sees public book/diary/document only; archived/private never inflate or add kinds
    assert hrefs == {"/opds/book", "/opds/diary", "/opds/document"}
    for entry in feed.findall(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        assert "kind=acquisition" in link.get("type")
        assert link.get("rel") == "subsection"
        assert entry.find(ATOM + "id") is not None and entry.find(ATOM + "updated") is not None


def test_root_for_private_caller_counts_private(env):
    feed = parse(env.c.get("/opds", headers=env.basic(env.owner)))
    entry = next(e for e in feed.findall(ATOM + "entry") if "diary" in e.find(ATOM + "id").text)
    assert "2" in entry.find(ATOM + "content").text  # one private + one public, archived hidden


def test_kind_feed_acquisition_entries(env):
    r = env.c.get("/opds/book", headers=env.basic(env.reader))
    assert "kind=acquisition" in r.headers["content-type"]
    feed = parse(r)
    (entry,) = feed.findall(ATOM + "entry")
    assert entry.find(ATOM + "title").text == "Moby Dick"
    assert entry.find(ATOM + "author/" + ATOM + "name").text == "Melville"
    assert entry.find(ATOM + "id").text == f"urn:culture-library:item:{env.book.id}"
    assert entry.find(ATOM + "updated").text
    (acq,) = [x for x in entry.findall(ATOM + "link") if x.get("rel") == ACQ]
    assert acq.get("type") == "application/epub+zip"
    got = env.c.get(acq.get("href"), headers=env.basic(env.reader))
    assert got.status_code == 200 and got.content == EPUB
    assert got.headers["content-type"].startswith("application/epub+zip")


def test_mime_types_per_format(env):
    pdf = parse(env.c.get("/opds/document", headers=env.basic(env.reader)))
    link = pdf.find(f"{ATOM}entry/{ATOM}link[@rel='{ACQ}']")
    assert link.get("type") == "application/pdf"
    md = parse(env.c.get("/opds/diary", headers=env.basic(env.reader)))
    link = md.find(f"{ATOM}entry/{ATOM}link[@rel='{ACQ}']")
    assert link.get("type") == "text/markdown"


def test_visibility_and_archive(env):
    titles = lambda key: [  # noqa: E731
        e.find(ATOM + "title").text
        for e in parse(env.c.get("/opds/diary", headers=env.basic(key))).findall(ATOM + "entry")
    ]
    assert titles(env.reader) == ["Pub Note"]
    assert sorted(titles(env.owner)) == ["Pub Note", "Secret/Note"]
    # newest first
    assert titles(env.owner) == ["Pub Note", "Secret/Note"]  # newest first
    r = env.c.get(f"/opds/item/{env.note.id}/file", headers=env.basic(env.reader))
    assert r.status_code == 404
    assert (
        env.c.get(f"/opds/item/{env.note.id}/file", headers=env.basic(env.owner)).status_code == 200
    )


def test_newest_first_and_paging(env):
    for i in range(55):
        env.add(f"# {i}".encode(), f"x{i}.md", f"T{i:02d}", "", "article", "public")
    feed = parse(env.c.get("/opds/article", headers=env.basic(env.reader)))
    titles = [e.find(ATOM + "title").text for e in feed.findall(ATOM + "entry")]
    assert len(titles) == 50 and titles[0] == "T54"
    rels = {link.get("rel"): link.get("href") for link in feed.findall(ATOM + "link")}
    assert "next" in rels and "previous" not in rels
    nxt = parse(env.c.get(rels["next"], headers=env.basic(env.reader)))
    assert len(nxt.findall(ATOM + "entry")) == 5
    assert "previous" in {link.get("rel") for link in nxt.findall(ATOM + "link")}


def test_unknown_kind_404_and_bad_page(env):
    assert env.c.get("/opds/nope", headers=env.basic(env.reader)).status_code == 404
    assert env.c.get("/opds/book?page=0", headers=env.basic(env.reader)).status_code == 422


def test_search_and_opensearch_description(env):
    desc = env.c.get("/opds/opensearch.xml", headers=env.basic(env.reader))
    root = parse(desc)
    assert root.tag == "{http://a9.com/-/spec/opensearch/1.1/}OpenSearchDescription"
    feed = parse(env.c.get("/opds/search?q=moby", headers=env.basic(env.reader)))
    assert [e.find(ATOM + "title").text for e in feed.findall(ATOM + "entry")] == ["Moby Dick"]
    feed = parse(env.c.get("/opds/search?q=secret", headers=env.basic(env.reader)))
    assert feed.findall(ATOM + "entry") == []


def test_title_is_escaped(env):
    env.add(b"# x", "x.md", "A & <B>", "C\"D'", "article", "public")
    feed = parse(env.c.get("/opds/article", headers=env.basic(env.reader)))
    assert feed.find(ATOM + "entry/" + ATOM + "title").text == "A & <B>"


# ------------------------------------------------------------------ WebDAV


def propfind(env, path, key=None, depth="1"):
    key = key or env.reader
    return env.c.request("PROPFIND", path, headers={**env.basic(key), "Depth": depth})


def responses(resp) -> dict[str, ET.Element]:
    assert resp.status_code == 207, resp.text
    root = parse(resp)
    assert root.tag == DAV + "multistatus"
    return {r.find(DAV + "href").text: r for r in root.findall(DAV + "response")}


def prop(response, name):
    return response.find(f"{DAV}propstat/{DAV}prop/{DAV}{name}")


def test_root_propfind_depth_1_and_0(env):
    got = responses(propfind(env, "/dav/"))
    assert "/dav/" in got and "/dav/book/" in got and "/dav/diary/" in got
    assert prop(got["/dav/book/"], "resourcetype").find(DAV + "collection") is not None
    assert prop(got["/dav/book/"], "displayname").text == "book"
    assert list(responses(propfind(env, "/dav", depth="0"))) == ["/dav/"]


def test_kind_listing_and_file_props(env):
    got = responses(propfind(env, "/dav/book/"))
    href = f"/dav/book/{env.book.id}-Moby Dick.epub".replace(" ", "%20")
    assert set(got) == {"/dav/book/", href}
    f = got[href]
    assert prop(f, "getcontentlength").text == str(env.book.size)
    assert prop(f, "getcontenttype").text == "application/epub+zip"
    assert prop(f, "getlastmodified").text.endswith("GMT")
    assert len(prop(f, "resourcetype")) == 0
    assert prop(f, "displayname").text == f"{env.book.id}-Moby Dick.epub"
    assert f.find(f"{DAV}propstat/{DAV}status").text == "HTTP/1.1 200 OK"


def test_dav_visibility_and_safe_titles(env):
    pub = responses(propfind(env, "/dav/diary/"))
    assert len(pub) == 2  # collection + the public note
    own = responses(propfind(env, "/dav/diary/", key=env.owner))
    names = sorted(prop(r, "displayname").text for r in own.values() if r is not None)
    assert f"{env.note.id}-Secret-Note.md" in names
    assert all("/" not in n for n in names)


def test_dav_get_head_bytes(env):
    path = f"/dav/book/{env.book.id}-Moby Dick.epub"
    r = env.c.get(path, headers=env.basic(env.reader))
    assert r.status_code == 200 and r.content == EPUB
    h = env.c.head(path, headers=env.basic(env.reader))
    assert h.status_code == 200 and h.headers["content-length"] == str(env.book.size)
    # wrong kind dir, unknown id, private
    assert (
        env.c.get(f"/dav/diary/{env.book.id}-x.epub", headers=env.basic(env.owner)).status_code
        == 404
    )
    assert env.c.get("/dav/book/9999-x.epub", headers=env.basic(env.reader)).status_code == 404
    private = f"/dav/diary/{env.note.id}-Secret-Note.md"
    assert env.c.get(private, headers=env.basic(env.reader)).status_code == 404
    assert env.c.get(private, headers=env.basic(env.owner)).status_code == 200
    assert propfind(env, private).status_code == 404


def test_dav_unknown_paths_404(env):
    assert propfind(env, "/dav/nope/").status_code == 404
    assert propfind(env, "/dav/book/zzz").status_code == 404


def test_file_propfind(env):
    path = f"/dav/book/{env.book.id}-Moby%20Dick.epub"
    got = responses(propfind(env, path, depth="0"))
    assert len(got) == 1


def test_options_advertises_read_only(env):
    r = env.c.options("/dav/", headers=env.basic(env.reader))
    assert r.status_code == 200
    assert r.headers["dav"].startswith("1")
    allow = {m.strip() for m in r.headers["allow"].split(",")}
    assert {"OPTIONS", "GET", "HEAD", "PROPFIND"} <= allow
    assert not allow & {"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK"}


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize("target", ["/dav/", "/dav/book/", "/dav/book/1-x.epub", "/dav/new.txt"])
def test_writes_are_405_even_for_owner(env, method, target):
    r = env.c.request(method, target, headers=env.basic(env.owner), content=b"x")
    assert r.status_code == 405
    allow = {m.strip() for m in r.headers["allow"].split(",")}
    assert allow == {"OPTIONS", "GET", "HEAD", "PROPFIND"}
    assert env.store.get_item(env.book.id) is not None


def test_dav_requires_read_scope(env):
    no_read(env)
    r = env.c.request("PROPFIND", "/dav/", headers=env.basic(env.reader))
    assert r.status_code == 403


def test_search_returns_the_newest_page_of_matches(env):
    added = [
        env.add(f"# w{i}".encode(), f"w{i}.md", f"Whale {i}", "", "diary", "public").id
        for i in range(60)
    ]
    feed = parse(env.c.get("/opds/search?q=whale", headers=env.basic(env.reader)))
    ids = [int(e.find(f"{ATOM}id").text.rsplit(":", 1)[1]) for e in feed.iter(f"{ATOM}entry")]
    assert ids == sorted(added, reverse=True)[:50]


def test_list_items_newest_first_is_opt_in(env):
    asc = library.list_items(env.store, include_private=True)
    desc = library.list_items(env.store, include_private=True, newest_first=True)
    assert [i.id for i in asc] == sorted(i.id for i in asc)
    assert [i.id for i in desc] == [i.id for i in reversed(asc)]
