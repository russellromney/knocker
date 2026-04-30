from __future__ import annotations

import time
from typing import Any


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


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
