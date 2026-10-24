"""Paths that must be covered in CI without poppler or events-cli installed."""

from __future__ import annotations

import stat
import sys
import types

import pytest

from culture_library.core import ingest, send


def _fake_exe(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="shell stand-in")
def test_pdf_text_goes_in_on_stdin(tmp_path, monkeypatch) -> None:
    # The stand-in fails unless it was given fd://0 and gets the PDF bytes on stdin.
    exe = _fake_exe(
        tmp_path,
        "pdftotext",
        'case "$*" in *"fd://0 -"*) ;; *) exit 9 ;; esac\n'
        'head -c 8 | grep -q "%PDF-1.4" || exit 8\n'
        'printf "Hello\\fworld"\n',
    )
    monkeypatch.setattr(ingest.shutil, "which", lambda name: str(exe))
    got = ingest._pdf_text(b"%PDF-1.4 rest", ingest.DEFAULT_LIMITS)
    assert got.text == "Hello\nworld" or got.text.split() == ["Hello", "world"]
    assert not got.reason


def test_pdf_text_reports_a_failing_extractor(tmp_path, monkeypatch) -> None:
    exe = _fake_exe(tmp_path, "pdftotext", "exit 3\n")
    monkeypatch.setattr(ingest.shutil, "which", lambda name: str(exe))
    got = ingest._pdf_text(b"%PDF-1.4", ingest.DEFAULT_LIMITS)
    assert (got.text, got.reason) == ("", "pdftotext_failed")


class _Result:
    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok, self.reason = ok, reason


def _install_fake_events_cli(monkeypatch, *, ok: bool = True, connected: bool = True):
    published: list = []

    class EventClient:
        def __init__(self) -> None:
            self.is_connected = connected
            self.closed = False

        def publish_event(self, env, topic, qos, wait):
            published.append((env, topic, qos))
            return _Result(ok, "" if ok else "broker said no")

        def close(self) -> None:
            self.closed = True

    class Envelope:
        @staticmethod
        def new(type_, source, data):
            return {"type": type_, "source": source, "data": data}

    pkg = types.ModuleType("events_cli")
    client_mod = types.ModuleType("events_cli.client")
    client_mod.EventClient = EventClient
    core = types.ModuleType("events_cli.core")
    env_mod = types.ModuleType("events_cli.core.envelope")
    env_mod.Envelope = Envelope
    topics = types.ModuleType("events_cli.core.topics")
    topics.type_to_topic = lambda t: "events/" + t.replace(".", "/")
    for name, mod in {
        "events_cli": pkg,
        "events_cli.client": client_mod,
        "events_cli.core": core,
        "events_cli.core.envelope": env_mod,
        "events_cli.core.topics": topics,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return EventClient, published


def test_publish_via_events_cli_python_api(monkeypatch) -> None:
    client_cls, published = _install_fake_events_cli(monkeypatch)
    assert send._import_client() is client_cls
    send._publish_api(client_cls, {"id": 1, "target": "x3-ori"})
    env, topic, qos = published[0]
    assert env["type"] == send.EVENT_TYPE
    assert env["source"] == send.SOURCE
    assert topic == "events/library/item/send_requested"


def test_publish_api_refusal_is_publish_unavailable(monkeypatch) -> None:
    client_cls, _ = _install_fake_events_cli(monkeypatch, ok=False)
    with pytest.raises(send.PublishUnavailable, match="broker said no"):
        send._publish_api(client_cls, {"id": 1})


def test_publish_api_not_connected_times_out(monkeypatch) -> None:
    client_cls, _ = _install_fake_events_cli(monkeypatch, connected=False)
    clock = iter([0.0, 10.0, 20.0])
    monkeypatch.setattr(send.time, "monotonic", lambda: next(clock))
    with pytest.raises(send.PublishUnavailable, match="not connected"):
        send._publish_api(client_cls, {"id": 1})
