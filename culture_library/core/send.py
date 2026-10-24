"""Send to device: build and publish the ``library.item.send_requested`` event.

The library's responsibility ends at the event (boundary c52). It publishes the event and
serves the content; culture-rules delivers it to a device and owns delivery status. This
module never calls a device or a device service and stores no device id or key.

Targets are a plain list of ``{"label", "enabled"}`` read from, in order of precedence,
the ``CULTURE_LIBRARY_SEND_TARGETS`` env var (a JSON list) or ``<data dir>/targets.json``.
Any other field in an entry (e.g. a stray device id) is dropped on load.

Publishing follows pocketsync: the ``events_cli`` Python API when importable, else the
``events`` console executable of events-cli by subprocess (fixed argv, no shell, timeout),
else :class:`PublishUnavailable`. No dependency is added; nothing here touches the network
directly (the broker connection lives inside events-cli).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CoreError, ValidationError
from .models import Item

EVENT_TYPE = "library.item.send_requested"
SOURCE = "agent://culture-library"  # events-cli requires an absolute URI
TARGETS_ENV = "CULTURE_LIBRARY_SEND_TARGETS"
LOCAL_URL_ENV = "CULTURE_LIBRARY_LOCAL_URL"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8796"
TARGETS_FILE = "targets.json"
EXECUTABLES = ("events", "events-cli")
PUBLISH_TIMEOUT = 15.0
_QOS = 1


class SendConfigError(CoreError):
    """The targets configuration is unreadable or malformed."""


class PublishUnavailable(CoreError):
    """The event could not be published (broker down, or events-cli not available)."""


class TargetRefused(ValidationError):
    """The requested target is unknown or disabled (``code`` says which)."""


@dataclass(frozen=True)
class Target:
    label: str
    enabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "enabled": self.enabled}


def load_targets(data_dir: str | os.PathLike[str] | None = None) -> list[Target]:
    """Targets from the env var, else ``<data_dir>/targets.json``, else none."""
    raw = os.environ.get(TARGETS_ENV)
    source = TARGETS_ENV
    if raw is None and data_dir is not None:
        path = Path(data_dir) / TARGETS_FILE
        if path.is_file():
            raw, source = path.read_text(encoding="utf-8"), str(path)
    if raw is None or not raw.strip():
        return []
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise SendConfigError(f"{source} is not valid JSON") from exc
    if not isinstance(entries, list):
        raise SendConfigError(f"{source} must be a JSON list of {{label, enabled}}")
    targets: list[Target] = []
    for entry in entries:
        label = entry.get("label") if isinstance(entry, dict) else None
        if not isinstance(label, str) or not label.strip():
            raise SendConfigError(f"{source}: every target needs a non-empty label")
        targets.append(Target(label.strip(), bool(entry.get("enabled", True))))
    return targets


def resolve_target(targets: list[Target], label: str) -> Target:
    for t in targets:
        if t.label == label:
            if not t.enabled:
                raise TargetRefused(f"target {label!r} is disabled", "target_disabled")
            return t
    raise TargetRefused(f"unknown target {label!r}", "unknown_target")


def local_base_url() -> str:
    return (os.environ.get(LOCAL_URL_ENV) or DEFAULT_LOCAL_URL).rstrip("/")


def build_event_data(item: Item, target: str, requester: str | None) -> dict[str, Any]:
    """The ``data`` of the event: the reference a rule needs to fetch metadata and bytes."""
    return {
        "id": item.id,
        "sha256": item.sha256,
        "kind": item.kind,
        "title": item.title,
        "format": item.format,
        "size": item.size,
        "content_url": f"{local_base_url()}/api/library/{item.id}/file",
        "target": target,
        "requester": requester,
    }


def _import_client() -> Any:
    try:
        from events_cli.client import EventClient  # noqa: PLC0415
    except ImportError:
        return None
    return EventClient


def _publish_api(client_cls: Any, data: dict[str, Any]) -> None:
    from events_cli.core.envelope import Envelope  # noqa: PLC0415
    from events_cli.core.topics import type_to_topic  # noqa: PLC0415

    client = client_cls()
    try:
        deadline = time.monotonic() + 5.0
        while hasattr(client, "is_connected") and not client.is_connected:
            if time.monotonic() >= deadline:
                raise PublishUnavailable("broker not connected")
            time.sleep(0.02)
        env = Envelope.new(EVENT_TYPE, SOURCE, data=data)
        result = client.publish_event(env, type_to_topic(EVENT_TYPE), qos=_QOS, wait=5.0)
        if not getattr(result, "ok", False):
            raise PublishUnavailable(f"publish refused: {getattr(result, 'reason', 'unknown')}")
    except PublishUnavailable:
        raise
    except Exception as exc:  # broker/library failure
        raise PublishUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if hasattr(client, "close"):
            client.close()


def _publish_cli(exe: str, data: dict[str, Any]) -> None:
    argv = [exe, "emit", EVENT_TYPE, "--data", "-", "--source", SOURCE, "--json"]
    try:
        done = subprocess.run(  # nosec B603
            argv,
            input=json.dumps(data),
            capture_output=True,
            text=True,
            timeout=PUBLISH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishUnavailable(f"events-cli failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        reason = (done.stderr or done.stdout or "").strip()[:200]
        raise PublishUnavailable(f"events-cli exited {done.returncode}: {reason}")


def publish_send_requested(data: dict[str, Any]) -> None:
    """Publish one event through events-cli; raise :class:`PublishUnavailable` on failure."""
    client_cls = _import_client()
    if client_cls is not None:
        return _publish_api(client_cls, data)
    for name in EXECUTABLES:
        exe = shutil.which(name)
        if exe:
            return _publish_cli(exe, data)
    raise PublishUnavailable("events-cli is not installed (no events_cli module or executable)")
