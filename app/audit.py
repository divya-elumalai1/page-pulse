import time
from datetime import datetime, timezone

import httpx


class AuditError(Exception):
    """Raised when a URL cannot be audited (unreachable, timeout, etc.)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


async def run_audit(url: str, timeout_seconds: float = 8.0) -> dict:
    """
    Fetch `url` and return structured audit data.

    Raises AuditError on timeout / connection failure / non-HTTP errors.
    Does NOT raise on 4xx/5xx responses from the target -- those are still
    valid audit results (the site responded, just with an error status).
    """
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=True
        ) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        raise AuditError("TIMEOUT", f"Request to {url} timed out after {timeout_seconds}s")
    except httpx.ConnectError:
        raise AuditError("CONNECTION_FAILED", f"Could not connect to {url}")
    except httpx.RequestError as e:
        raise AuditError("REQUEST_FAILED", f"Request to {url} failed: {e}")

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

    return {
        "url": url,
        "status_code": response.status_code,
        "response_time_ms": elapsed_ms,
        "content_length_bytes": len(response.content) if response.content else None,
        "server_header": response.headers.get("server"),
        "content_type": response.headers.get("content-type"),
        "from_cache": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
