"""Serve TusASGIApp directly via uvicorn.

Unlike ``fastapi_app.py`` (which wraps ``handle_request`` in a thin FastAPI
route), this example hands the ASGI adapter straight to uvicorn so the TUS
server participates in the ASGI pipeline with no intermediate framework.
The sync handler runs on a worker thread via ``asyncio.to_thread``, keeping
the event loop free.

Good when you want a single-purpose TUS endpoint without the weight of
Flask/FastAPI, but still need ASGI features (HTTP/2 via uvicorn, reverse-
proxy-friendly, graceful shutdown, etc.).

Run::

    pip install uvicorn
    python examples/server/asgi_app.py          # → :8000
    python examples/server/asgi_app.py 9000     # → :9000
"""

from __future__ import annotations

import sys

import uvicorn

from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus = TusServer(
    storage=storage,
    base_path="/files",
    max_size=100 * 1024 * 1024,
    upload_expiry=3600,
    cors_allow_origins="*",
)

# `app` is itself the ASGI application — uvicorn routes everything to it.
app = TusASGIApp(tus)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
