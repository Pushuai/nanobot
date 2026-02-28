"""Protocol helpers for desktop WebSocket API."""

from __future__ import annotations

from typing import Any


API_VERSION = "1.0"


def make_response(
    request_id: Any,
    ok: bool,
    data: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": request_id,
        "type": "response",
        "ok": ok,
    }
    if ok:
        payload["data"] = data
    else:
        payload["error"] = error or "unknown error"
    return payload


def make_event(event: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "event",
        "event": event,
    }
    if data is not None:
        payload["data"] = data
    return payload


def parse_request(payload: Any) -> tuple[Any, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("invalid payload: expected object")
    req_id = payload.get("id")
    action = payload.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("invalid request: missing action")
    raw = payload.get("payload") or {}
    if not isinstance(raw, dict):
        raise ValueError("invalid request: payload must be object")
    return req_id, action.strip(), raw
