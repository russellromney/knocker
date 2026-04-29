import asyncio
import hashlib
import hmac
import time

import knocker


def generic_hmac_signature(secret: str, body: bytes, *, prefix: str = "sha256=") -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


def stripe_signature(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def require_event_id(result: knocker.IngestResult) -> int:
    assert result.event_id is not None
    return result.event_id


async def wait_for_status(app, event_id: int, status: str, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"event {event_id} did not reach status {status!r}")
