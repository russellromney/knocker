from __future__ import annotations

import json
import time
from typing import Any, Optional


def _coerce_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    limit = value
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return limit


def _coerce_older_than(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("older_than must be an integer timestamp")
    return value


def _coerce_since(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("since must be an integer timestamp")
    return value


def _coerce_bool_filter(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
    return value


def _coerce_prune_statuses(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError("statuses must be a non-empty list or tuple of strings")
    if not isinstance(value, (list, tuple)):
        raise TypeError("statuses must be a non-empty list or tuple of strings")
    if not value:
        raise ValueError("statuses must be a non-empty list or tuple")
    allowed = {"handled", "ignored"}
    ordered: list[str] = []
    seen: set[str] = set()
    for status in value:
        if not isinstance(status, str):
            raise TypeError("statuses must contain only strings")
        if status not in allowed:
            raise ValueError("statuses must contain only 'handled' or 'ignored'")
        if status not in seen:
            ordered.append(status)
            seen.add(status)
    return tuple(ordered)


def _sql_placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _event_id_from_payload_json(payload: Any) -> Optional[int]:
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
    if name not in payload:
        return None
    return _required_payload_int(payload, name)


def _required_payload_int(payload: dict[str, Any], name: str) -> int:
    value = payload[name]
    if isinstance(value, bool):
        raise ValueError(f"job payload {name} must be an integer")
    if not isinstance(value, int):
        raise ValueError(f"job payload {name} must be an integer")
    return value


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
