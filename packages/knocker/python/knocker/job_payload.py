from __future__ import annotations

import json
from typing import Any, Optional


def _event_id_from_payload_json(payload: Any) -> Optional[int]:
    """Extract the ``event_id`` integer from a serialized Honker job payload.

    Returns ``None`` for malformed bytes/strings, non-JSON content, missing
    field, or non-integer values. Retention live-job cleanup relies on this
    treating malformed payloads as non-matches rather than coercing strings.
    """

    if isinstance(payload, bytes):
        try:
            payload_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(payload, str):
        payload_text = payload
    else:
        return None
    try:
        value = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or "event_id" not in value:
        return None
    event_id = value["event_id"]
    if isinstance(event_id, bool):
        return None
    try:
        return int(event_id)
    except (TypeError, ValueError):
        return None


def _optional_payload_int(payload: dict[str, Any], name: str) -> Optional[int]:
    """Return an optional integer payload field, or ``None`` if absent."""

    if name not in payload:
        return None
    return _required_payload_int(payload, name)


def _required_payload_int(payload: dict[str, Any], name: str) -> int:
    """Return a required integer payload field, raising on bool or non-int."""

    value = payload[name]
    if isinstance(value, bool):
        raise ValueError(f"job payload {name} must be an integer")
    if not isinstance(value, int):
        raise ValueError(f"job payload {name} must be an integer")
    return value
