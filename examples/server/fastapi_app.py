#!/usr/bin/env python3
"""FastAPI integration example for TUS server.

Install: pip install fastapi uvicorn
Run    : python examples/server/fastapi_app.py [port]

    python examples/server/fastapi_app.py        # default port 8000
    python examples/server/fastapi_app.py 9000   # custom port
"""

import logging
import sys

import uvicorn
from fastapi import FastAPI, Request, Response

from resumable_upload import SQLiteStorage, TusServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(title="TUS Upload Server")

storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus_server = TusServer(
    storage=storage,
    base_path="/files",
    max_size=100 * 1024 * 1024,  # 100 MB
    upload_expiry=3600,  # 1 hour
    cleanup_interval=300,  # clean up every 5 minutes
    cors_allow_origins="*",  # restrict in production
)


async def _handle(request: Request) -> Response:
    body = await request.body()
    status, resp_headers, resp_body = tus_server.handle_request(
        request.method, request.url.path, dict(request.headers), body
    )
    return Response(content=resp_body, status_code=status, headers=resp_headers)


@app.options("/files")
@app.post("/files")
async def create_upload(request: Request):
    return await _handle(request)


@app.head("/files/{upload_id}")
@app.patch("/files/{upload_id}")
@app.delete("/files/{upload_id}")
async def manage_upload(upload_id: str, request: Request):
    return await _handle(request)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"TUS server (FastAPI) running on http://localhost:{port}")
    print(f"Upload endpoint: http://localhost:{port}/files")
    print(f"API docs: http://localhost:{port}/docs")
    print("Press Ctrl+C to stop")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
