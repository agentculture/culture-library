from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from culture_library.core import send
from culture_library.core.models import Item

ITEM = Item(
    id=7,
    sha256="a" * 64,
    kind="book",
    title="Dune",
    author="FH",
    format="epub",
    size=123,
    visibility="private",
    created_at="2026-01-01T00:00:00Z",
)


def test_targets_default_empty(monkeypatch, tmp_path):
    monkeypatch.delenv(send.TARGETS_ENV, raising=False)
    assert send.load_targets(tmp_path) == []


def test_targets_from_env_and_file(monkeypatch, tmp_path):
    (tmp_path / "targets.json").write_text('[{"label": "file-one", "enabled": true}]')
    assert [t.label for t in send.load_targets(tmp_path)] == ["file-one"]
    monkeypatch.setenv(
        send.TARGETS_ENV,
        '[{"label":"x3-ori","enabled":true},{"label":"reTerminal Sticky","enabled":false}]',
    )
    got = send.load_targets(tmp_path)
    assert [(t.label, t.enabled) for t in got] == [("x3-ori", True), ("reTerminal Sticky", False)]


def test_targets_strip_unknown_fields(monkeypatch, tmp_path):
    monkeypatch.setenv(
        send.TARGETS_ENV, '[{"label":"a","enabled":true,"device_id":"SECRET","key":"k"}]'
    )
    assert send.load_targets(tmp_path)[0].as_dict() == {"label": "a", "type": "", "enabled": True}


def test_targets_malformed(monkeypatch, tmp_path):
    monkeypatch.setenv(send.TARGETS_ENV, "{nope")
    with pytest.raises(send.SendConfigError):
        send.load_targets(tmp_path)


def test_resolve_target_refusals():
    ts = [send.Target("a", True), send.Target("b", False)]
    assert send.resolve_target(ts, "a").label == "a"
    with pytest.raises(send.TargetRefused) as e:
        send.resolve_target(ts, "b")
    assert e.value.code == "target_disabled"
    with pytest.raises(send.TargetRefused) as e:
        send.resolve_target(ts, "zzz")
    assert e.value.code == "unknown_target"


def test_build_event_payload(monkeypatch):
    monkeypatch.delenv(send.LOCAL_URL_ENV, raising=False)
    data = send.build_event_data(ITEM, "x3-ori", "reader-key")
    assert data == {
        "id": 7,
        "sha256": "a" * 64,
        "kind": "book",
        "title": "Dune",
        "format": "epub",
        "size": 123,
        "content_url": "http://127.0.0.1:8796/api/library/7/file",
        "target": "x3-ori",
        "requester": "reader-key",
    }


def test_local_url_env(monkeypatch):
    monkeypatch.setenv(send.LOCAL_URL_ENV, "http://10.0.0.5:9000/")
    assert send.build_event_data(ITEM, "t", None)["content_url"] == (
        "http://10.0.0.5:9000/api/library/7/file"
    )


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_cli_publisher_argv(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _Done()

    monkeypatch.setattr(send, "_import_client", lambda: None)
    monkeypatch.setattr(send.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(send.subprocess, "run", run)
    send.publish_send_requested({"id": 1})
    assert seen["argv"][1:4] == ["emit", send.EVENT_TYPE, "--data"]
    assert "--source" in seen["argv"]
    assert send.SOURCE in seen["argv"]
    assert json.loads(seen["kw"]["input"]) == {"id": 1}
    assert seen["kw"]["timeout"]
    assert not seen["kw"].get("shell")


def test_cli_publisher_failure(monkeypatch):
    monkeypatch.setattr(send, "_import_client", lambda: None)
    monkeypatch.setattr(send.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(send.subprocess, "run", lambda *a, **k: _Done(2, "", "broker down"))
    with pytest.raises(send.PublishUnavailable):
        send.publish_send_requested({"id": 1})


def test_cli_publisher_timeout(monkeypatch):
    def run(*a, **k):
        raise subprocess.TimeoutExpired("events", 1)

    monkeypatch.setattr(send, "_import_client", lambda: None)
    monkeypatch.setattr(send.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(send.subprocess, "run", run)
    with pytest.raises(send.PublishUnavailable):
        send.publish_send_requested({"id": 1})


def test_neither_available(monkeypatch):
    monkeypatch.setattr(send, "_import_client", lambda: None)
    monkeypatch.setattr(send.shutil, "which", lambda n: None)
    with pytest.raises(send.PublishUnavailable):
        send.publish_send_requested({"id": 1})


def test_no_device_http_calls():
    root = Path(send.__file__).parents[1]
    for py in root.rglob("*.py"):
        text = py.read_text().lower()
        if py.name == "send.py" and py.parent.name == "core":
            # the module may mention the names in prose only; no URL/host usage
            assert "http://xteink" not in text
            assert "https://xteink" not in text
            continue
        for bad in ("xteink.", "reterminal."):
            assert f"http://{bad}" not in text, py
            assert f"https://{bad}" not in text, py
        assert "requests.post" not in text, py
        assert "httpx" not in text, py


# ------------------------------------------------------------------ type label (d4 item 3)


def test_targets_carry_an_optional_free_text_type(monkeypatch, tmp_path):
    monkeypatch.setenv(
        send.TARGETS_ENV,
        '[{"label":"x3-ori","type":"Xteink X3 reader","enabled":true,"device_id":"SECRET"},'
        '{"label":"reTerminal Sticky","type":"  e-paper board ","enabled":false},'
        '{"label":"plain"}]',
    )
    got = [t.as_dict() for t in send.load_targets(tmp_path)]
    assert got == [
        {"label": "x3-ori", "type": "Xteink X3 reader", "enabled": True},
        {"label": "reTerminal Sticky", "type": "e-paper board", "enabled": False},
        {"label": "plain", "type": "", "enabled": True},
    ]


def test_target_type_must_be_text(monkeypatch, tmp_path):
    monkeypatch.setenv(send.TARGETS_ENV, '[{"label":"a","type":42}]')
    with pytest.raises(send.SendConfigError, match="type"):
        send.load_targets(tmp_path)
