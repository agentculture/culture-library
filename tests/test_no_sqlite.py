"""No SQLite left in the package (plan library-on-postgres-and-pgvector, task t16, criterion 1).

Scans every ``culture_library/**/*.py`` with :mod:`ast` (not a line grep, so comments and
documentation never trip it and regexes or URLs holding ``?`` are told apart from SQL) and
every ``culture_library/**/*.sql`` as text, and fails on:

* ``import sqlite3`` / ``from sqlite3 import ...``, or the string ``"sqlite3"`` anywhere in
  code (``importlib.import_module("sqlite3")``);
* ``.lastrowid`` (attribute access);
* in any string that code uses (docstrings and other bare string statements are documentation
  and skipped): ``AUTOINCREMENT``, ``json_extract``, ``INSERT OR REPLACE``,
  ``INSERT OR IGNORE``, ``sqlite_master``, ``PRAGMA`` (any case);
* ``?`` placeholders in SQL. A string (an f-string's literal parts joined, or a ``+`` chain of
  literals, counts as one) is SQL when it holds an uppercase SQL keyword as a word (the
  package writes SQL keywords in uppercase; ``AND``, ``WHERE``, ``SET`` and the like cover
  fragments). In SQL, a ``?`` is a placeholder when it follows ``=``, ``<``, ``>``, ``(``,
  ``,`` or the word ``IN``/``LIKE``/``LIMIT``/``OFFSET`` (whitespace allowed between) and is
  followed by whitespace, ``)``, ``,``, ``;`` or the end. So ``(?:``, ``(?P<``, ``x?``,
  ``)?`` and ``/path?q=`` never count; Postgres's jsonb ``?`` operator is not used.

``.sql`` files are checked for the keyword list with ``--`` comments removed, but not for
``?`` (in Postgres SQL it is the jsonb operator, never a placeholder).

Exempt by name: ``migrate/xteink.py`` (reads xteink's SQLite database) and
``migrate/sqlite_to_pg.py`` (reads the SQLite library at cutover, plan task t14; it may not
exist yet on a branch). They are the only modules that may talk SQLite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "culture_library"
EXEMPT = frozenset({"migrate/xteink.py", "migrate/sqlite_to_pg.py"})

_BANNED = re.compile(
    r"AUTOINCREMENT|json_extract|INSERT\s+OR\s+(?:REPLACE|IGNORE)|sqlite_master|\bPRAGMA\b",
    re.IGNORECASE,
)
_SQL_WORD = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|AND|OR|SET|VALUES|LIMIT|OFFSET|IN|JOIN|ON|"
    r"RETURNING|CREATE|ALTER|DROP|ORDER BY|GROUP BY|HAVING|CONFLICT)\b"
)
_PLACEHOLDER = re.compile(r"(?:[=<>(,]|\b(?:IN|LIKE|LIMIT|OFFSET)\b)\s*\?(?=\s|[),;]|$)")


def _literal(node: ast.AST) -> str | None:
    """The text of a string literal, an f-string (``{}`` for each field) or a ``+`` chain."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def is_sql_placeholder(text: str) -> bool:
    """True when ``text`` reads as SQL and holds a ``?`` placeholder (see the module doc)."""
    return bool(_SQL_WORD.search(text)) and bool(_PLACEHOLDER.search(text))


def findings(source: str, name: str = "<src>") -> list[str]:
    """Every banned construct in one module's source, as ``name:line: what`` strings."""
    tree = ast.parse(source, name)
    docs = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Constant, ast.JoinedStr))
    }
    out: list[str] = []

    def hit(node: ast.AST, what: str) -> None:
        out.append(f"{name}:{getattr(node, 'lineno', '?')}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "sqlite3" for a in node.names):
                hit(node, "import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "sqlite3":
                hit(node, "from sqlite3 import")
        elif isinstance(node, ast.Attribute) and node.attr == "lastrowid":
            hit(node, "lastrowid")
        if id(node) in docs:
            continue
        text = _literal(node)
        if text is None:
            continue
        if text.strip() == "sqlite3":
            hit(node, 'the string "sqlite3"')
        banned = _BANNED.search(text)
        if banned:
            hit(node, f"SQLite SQL {banned.group(0)!r}")
        if is_sql_placeholder(text):
            hit(node, f"? placeholder in SQL {text.strip()[:60]!r}")
    return out


def sql_file_findings(text: str, name: str = "<sql>") -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        code = line.split("--", 1)[0]
        banned = _BANNED.search(code)
        if banned:
            out.append(f"{name}:{n}: SQLite SQL {banned.group(0)!r}")
    return out


def _modules() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if p.relative_to(PKG).as_posix() not in EXEMPT)


def test_the_package_has_no_sqlite() -> None:
    found: list[str] = []
    for path in _modules():
        rel = path.relative_to(PKG).as_posix()
        found += findings(path.read_text(encoding="utf-8"), rel)
    for path in sorted(PKG.rglob("*.sql")):
        rel = path.relative_to(PKG).as_posix()
        found += sql_file_findings(path.read_text(encoding="utf-8"), rel)
    assert found == [], "SQLite constructs left in culture_library/:\n" + "\n".join(found)


def test_the_scan_covers_the_package_and_exempts_only_the_two_migrators() -> None:
    scanned = {p.relative_to(PKG).as_posix() for p in _modules()}
    assert {"core/store.py", "core/search.py", "server/app.py"} <= scanned
    assert not scanned & EXEMPT
    every = {p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py")}
    assert every - scanned <= EXEMPT


# -- the checker itself: what it must catch, and the look-alikes it must leave alone


@pytest.mark.parametrize(
    "src, what",
    [
        ("import sqlite3\n", "import sqlite3"),
        ("import sqlite3 as db\n", "import sqlite3"),
        ("from sqlite3 import connect\n", "from sqlite3 import"),
        ("import importlib\nimportlib.import_module('sqlite3')\n", '"sqlite3"'),
        ("n = cur.lastrowid\n", "lastrowid"),
        ("q = 'CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)'\n", "AUTOINCREMENT"),
        ("q = \"SELECT json_extract(payload, '$.a') FROM feed\"\n", "json_extract"),
        ("q = 'INSERT OR REPLACE INTO t VALUES (%s)'\n", "INSERT OR REPLACE"),
        ("q = 'insert or ignore into t values (%s)'\n", "insert or ignore"),
        ('q = "SELECT sql FROM sqlite_master WHERE name = %s"\n', "sqlite_master"),
        ("conn.execute('PRAGMA foreign_keys = ON')\n", "PRAGMA"),
        ("conn.execute('SELECT * FROM items WHERE id = ?', (1,))\n", "placeholder"),
        ("conn.execute('INSERT INTO t (a, b) VALUES (?, ?)', r)\n", "placeholder"),
        ("w = ' AND i.kind = ?'\n", "placeholder"),
        ("w = f' AND {col} IN (?, ?)'\n", "placeholder"),
        ("q = 'SELECT a FROM t ' + 'WHERE b = ?'\n", "placeholder"),
        ("q = 'SELECT a FROM t LIMIT ?'\n", "placeholder"),
    ],
)
def test_the_checker_catches(src: str, what: str) -> None:
    got = findings(src)
    assert len(got) >= 1, got
    assert any(what.lower() in g.lower() for g in got), got


@pytest.mark.parametrize(
    "src",
    [
        "'''Docstring: we used to say INSERT OR REPLACE, PRAGMA and sqlite_master.'''\n",
        "def f():\n    '''Like AUTOINCREMENT; like sqlite3.Row; WHERE id = ?.'''\n",
        "# import sqlite3 -- a comment\nx = 1\n",
        "conn.execute('SELECT * FROM items WHERE id = %s', (1,))\n",
        "r = re.compile(r'(?:[^@/:\\s]+@)?(?P<host>[^@/:\\s]+):(?P<path>.*)$')\n",
        "r = re.compile(r'\\(?\\d{4}-\\d{2}-\\d{2}\\)?\\Z')\n",
        "r = re.compile(r'(^|/)(nav|toc)\\.x?html?$', re.I)\n",
        "r = re.compile(r'\\b(?:the|this)\\s+(?!\\s+(?:of|on|in|by)\\b)')\n",
        "u = f'/api/eval/compare?before={b}&after={a}'\n",
        "u = '/opds/search?q={searchTerms}'\n",
        'x = \'<?xml version="1.0" encoding="UTF-8"?>\'\n',
        "p.add_argument('x', nargs='?')\n",
        "v = s.get('version', '?')\n",
        "t = 'Did a change help? Compare two runs (or groups)'\n",
        "q = \"SELECT payload ? 'a' FROM feed\"\n",  # jsonb key-exists, no operator before it
    ],
)
def test_the_checker_leaves_alone(src: str) -> None:
    assert findings(src) == []


def test_sql_files_ignore_comments_but_not_code() -> None:
    assert sql_file_findings("-- once: PRAGMA foreign_keys\nCREATE TABLE t (id int);\n") == []
    assert sql_file_findings("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT);\n")
