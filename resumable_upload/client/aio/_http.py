"""Lazy httpx import + request/error mapping for the async client."""

from __future__ import annotations

from typing import Any

from resumable_upload.exceptions import TusCommunicationError


def import_httpx() -> Any:
    try:
        import httpx
    except ModuleNotFoundError as e:  # pragma: no cover - exercised via subprocess
        raise ImportError(
            "The async client requires httpx. Install it with: "
            "pip install resumable-upload[async]"
        ) from e
    return httpx


async def request(
    client: Any, method: str, url: str, *, headers: dict, content: bytes = b""
) -> Any:
    """Issue a request; map transport errors to TusCommunicationError.

    Does NOT raise for HTTP status codes — the caller inspects status_code so
    it can implement TUS-specific handling (404 tolerated on DELETE, 409 → resync).
    """
    httpx = import_httpx()
    try:
        return await client.request(method, url, headers=headers, content=content)
    except httpx.HTTPError as e:
        raise TusCommunicationError(f"{method} {url} failed: {e}") from e
