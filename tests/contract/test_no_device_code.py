"""Scope guard: xteink's reader-device surface was not carried into culture-library.

xteink (ea2f643) also has device keys (``xtd_``), a device app (``/api/device/*`` and
``/api/devices*``), a per-device delivery queue and reader firmware. The library cites only
the catalog; this scans every source file under ``culture_library/`` for those artefacts.
Provenance comments that say they were *dropped* are prose, not code, and are allowed.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "culture_library"

FORBIDDEN = {
    "device key prefix": re.compile(r"xtd_"),
    "device route": re.compile(r"/api/devices?\b"),
    "device/queue table": re.compile(
        r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?\"?(devices?|queue|device_\w+|deliver\w*)\b",
        re.IGNORECASE,
    ),
    "queue model": re.compile(r"\bclass\s+(Device|QueueEntry|DeviceQueue)\b"),
    "firmware code": re.compile(r"firmware", re.IGNORECASE),
}


def _sources() -> list[Path]:
    return sorted(
        p
        for p in PKG.rglob("*")
        if p.is_file() and p.suffix in {".py", ".sql", ".json", ".html", ".js", ".toml", ".yaml"}
    )


def test_scan_covers_the_package():
    names = {p.name for p in _sources()}
    assert {"library.py", "routes_library.py", "store.py"} <= names


def test_no_device_queue_or_firmware_code():
    hits = []
    for path in _sources():
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for what, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    hits.append(f"{path.relative_to(PKG.parent)}:{lineno}: {what}: {line.strip()}")
    assert hits == []


def test_patterns_would_catch_xteink_device_code():
    samples = [
        'DEVICE_PREFIX = "xtd_"',
        '@router.get("/api/device/queue")',
        "CREATE TABLE IF NOT EXISTS devices (",
        "CREATE TABLE queue (",
        "class QueueEntry:",
        "def flash_firmware(port):",
    ]
    for sample in samples:
        assert any(p.search(sample) for p in FORBIDDEN.values()), sample


def test_no_device_routes_registered():
    import pytest

    pytest.importorskip("fastapi")
    from culture_library.server.app import create_app

    paths = set(create_app().openapi()["paths"])
    assert not [p for p in paths if "device" in p or "firmware" in p]
    assert "/api/library" in paths
