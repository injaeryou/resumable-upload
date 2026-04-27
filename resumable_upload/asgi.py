"""ASGI adapter wrapping TusServer.handle_request under asyncio.to_thread.

Gives users FastAPI / Starlette compatibility without having to rewrite
``TusServer`` as async. The sync handler runs on a thread pool, so the event
loop stays free. This keeps Phase B scoped — an async-native storage rewrite
can come later without breaking this interface.

Usage::

    from fastapi import FastAPI
    from resumable_upload import SQLiteStorage, TusServer
    from resumable_upload.asgi import TusASGIApp

    app = FastAPI()
    tus = TusServer(storage=SQLiteStorage(), base_path="/files")
    app.mount("/files", TusASGIApp(tus))
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, Callable

from resumable_upload.server import TusServer

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TusASGIApp:
    """ASGI application that delegates to a synchronous ``TusServer``."""

    def __init__(self, server: TusServer) -> None:
        self._server = server

    def _handle_get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        if self._server.metrics is not None and path == self._server.metrics_path:
            body = self._server.metrics.render().encode("utf-8")
            return (
                200,
                {
                    "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
                    "Content-Length": str(len(body)),
                },
                body,
            )
        return (404, {}, b"")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # No lifespan events to manage — accept and idle.
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            raise NotImplementedError(
                f"TusASGIApp only handles http and lifespan scopes, got {scope['type']!r}"
            )

        method = scope["method"]
        path = scope["path"]
        raw_headers = scope.get("headers", [])
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in raw_headers}

        # Drain the request body. For PATCH/POST this gives the handler the
        # complete byte buffer; the sync handler doesn't support streaming.
        body_chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.request":
                body_chunks.append(message.get("body", b"") or b"")
                more = bool(message.get("more_body", False))
            elif message["type"] == "http.disconnect":
                return
        body = b"".join(body_chunks)

        if method == "GET":
            status, resp_headers, resp_body = self._handle_get(path)
        else:
            status, resp_headers, resp_body = await asyncio.to_thread(
                self._server.handle_request, method, path, headers, body
            )

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in resp_headers.items()
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": resp_body,
                "more_body": False,
            }
        )
