"""``POST /api/library/{id}/send`` and ``GET /api/send/targets``.

Publishes one ``library.item.send_requested`` event and appends it to the feed. The library
does nothing further: delivery and its status belong to culture-rules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from culture_library.core import NotFoundError, Store, library, send
from culture_library.core.feed import append
from culture_library.server.auth import Principal, check, current_principal, get_store

router = APIRouter()
publish = send.publish_send_requested  # tests replace this


class SendBody(BaseModel):
    target: str


def _error(status: int, detail: str, code: str) -> HTTPException:
    return HTTPException(status, detail={"detail": detail, "code": code})


def _targets(store: Store) -> list[send.Target]:
    try:
        return send.load_targets(store.data_dir)
    except send.SendConfigError as exc:
        raise _error(500, str(exc), "bad_send_config") from None


@router.get("/api/send/targets", tags=["send"])
def targets(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    check(principal, "read")
    return {"targets": [t.as_dict() for t in _targets(get_store(request))]}


@router.post("/api/library/{item_id}/send", status_code=202, tags=["send"])
def send_item(
    item_id: int,
    body: SendBody,
    request: Request,
    principal: Principal = Depends(current_principal),
) -> dict:
    check(principal, "write")
    store = get_store(request)
    try:
        item = library.get(
            store, item_id, include_private=principal.private, owner=principal.key_id
        )
    except NotFoundError:
        raise _error(404, f"item {item_id} not found", "not_found") from None
    try:
        send.resolve_target(_targets(store), body.target)
    except send.TargetRefused as exc:
        raise _error(422, str(exc), exc.code) from None
    data = send.build_event_data(item, body.target, principal.key_id)
    try:
        publish(data)
    except send.PublishUnavailable:
        raise _error(
            503, "the event broker is unavailable; retry shortly", "publish_unavailable"
        ) from None
    with store.connect() as conn:
        append(
            conn,
            {
                "type": "send_requested",
                "item_id": item.id,
                "sha256": item.sha256,
                "kind": item.kind,
                "visibility": item.visibility,
                "data": {"target": body.target, "requester": principal.key_id},
            },
        )
    return {"status": "requested", "item_id": item.id, "target": body.target}
