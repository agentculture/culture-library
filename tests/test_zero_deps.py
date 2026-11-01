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
        top = name.split('.')[0]
        if top in {'fastapi', 'uvicorn', 'starlette', 'mcp', 'multipart', 'httpx',
                   'ragas', 'openai', 'datasets', 'instructor',
                   'boto3', 'botocore'} or top.startswith('langchain'):
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


_FRESH = """
import importlib, importlib.util, pkgutil, sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec('fastapi') is None, 'fresh venv must not have the [server] extra'
import culture_library.cli as cli
for m in pkgutil.walk_packages(cli.__path__, 'culture_library.cli.'):
    importlib.import_module(m.name)
from culture_library.cli import main
sys.exit(0 if main(['server', 'serve', '--port', '1']) == 2 else 99)
"""


def test_cli_imports_in_fresh_venv_without_extra(tmp_path: Path) -> None:
    """A venv with nothing installed (so no [server] extra) imports and runs the CLI."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    proc = subprocess.run(
        [str(py), "-c", _FRESH, str(ROOT)], capture_output=True, text=True, cwd=tmp_path
    )
    assert proc.returncode == 0, proc.stderr
    assert "culture-library[server]" in proc.stderr


def test_eval_extra_pinned_and_not_in_dev() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []
    assert data["project"]["optional-dependencies"]["eval"] == [
        "ragas==0.4.3",
        "openai>=1",
        "langchain-community<0.4.2",
    ]
    dev = " ".join(data["dependency-groups"]["dev"])
    assert "ragas" not in dev
    assert "ragas" in " ".join(data["tool"]["pytest"]["ini_options"]["markers"])


def test_default_ci_sync_has_no_eval_extra() -> None:
    wf = (ROOT / ".github/workflows/tests.yml").read_text()
    syncs = [ln.strip() for ln in wf.splitlines() if "uv sync" in ln]
    assert "- run: uv sync" in syncs
    assert [s for s in syncs if "eval" in s] == ["- run: uv sync --extra eval"]


def test_s3_extra_declared_and_base_still_empty() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []
    assert any(d.startswith("boto3") for d in data["project"]["optional-dependencies"]["s3"])
    assert ".[server,s3,pg]" in (ROOT / "deploy/Dockerfile").read_text()


def test_pg_extra_is_psycopg3_and_the_image_installs_it() -> None:
    """The store's driver is the optional [pg] extra (psycopg 3), never a base dependency;
    the server image installs it beside [server] and [s3]."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == []
    pg = data["project"]["optional-dependencies"]["pg"]
    assert any(d.startswith("psycopg[binary]>=3") for d in pg), pg
    install = [
        ln for ln in (ROOT / "deploy/Dockerfile").read_text().splitlines() if "pip install" in ln
    ]
    assert len(install) == 1, install
    assert '".[server,s3,pg]"' in install[0], install
