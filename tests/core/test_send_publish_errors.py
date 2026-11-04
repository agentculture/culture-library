"""A bad broker setting is the designed 503 (``PublishUnavailable``), not an unhandled 500."""

import sys
import types

import pytest

from culture_library.core import send


def test_a_client_that_cannot_be_built_is_publish_unavailable(monkeypatch):
    def bad_client():
        raise ValueError("EVENTS_BROKER_PORT is not a port")

    for name in ("events_cli", "events_cli.core"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    envelope = types.ModuleType("events_cli.core.envelope")
    envelope.Envelope = object
    topics = types.ModuleType("events_cli.core.topics")
    topics.type_to_topic = str
    monkeypatch.setitem(sys.modules, envelope.__name__, envelope)
    monkeypatch.setitem(sys.modules, topics.__name__, topics)
    with pytest.raises(send.PublishUnavailable):
        send._publish_api(bad_client, {})
