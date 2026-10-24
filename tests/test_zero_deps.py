"""The CLI must import and run without the optional ``[server]`` extra."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = """
import importlib, pkgutil, sys

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in {'fastapi', 'uvicorn', 'starlette', 'mcp', 'multipart', 'httpx'}:
            raise ImportError('blocked: ' + name)

sys.meta_path.insert(0, Block())
import culture_library.cli as cli
for m in pkgutil.walk_packages(cli.__path__, 'culture_library.cli.'):
    importlib.import_module(m.name)
from culture_library.cli import main
rc = main(['server', 'serve', '--port', '1'])
sys.exit(0 if rc == 2 else 99)
"""


def test_cli_imports_without_server_extra() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "culture-library[server]" in proc.stderr


def test_base_dependencies_empty_and_server_extra() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []
    extra = " ".join(data["project"]["optional-dependencies"]["server"])
    for name in ("fastapi", "uvicorn", "mcp>=1.2,<2", "python-multipart"):
        assert name in extra


def test_claude_md_records_server_extra() -> None:
    assert "[server]" in (ROOT / "CLAUDE.md").read_text()
