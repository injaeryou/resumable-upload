# FastAPI

FastAPI users have two integrations to choose from. Pick **ASGI mount** unless you have a reason to handle the request manually.

## Option 1 — ASGI mount (recommended)

`TusASGIApp` runs the synchronous `TusServer.handle_request` on a thread pool, so the event loop stays free. One line to wire up:

```python
from fastapi import FastAPI
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

app = FastAPI()
tus = TusServer(storage=SQLiteStorage(), base_path="/files")
app.mount("/files", TusASGIApp(tus))
```

See [ASGI](asgi.md) for the full adapter reference (Starlette, Quart, custom mount paths).

## Option 2 — Manual route

If you need to wrap the request in middleware, dependencies, or a custom auth flow, call `handle_request` yourself:

```python
from fastapi import FastAPI, Request, Response
from resumable_upload import TusServer, SQLiteStorage

app = FastAPI()
tus_server = TusServer(storage=SQLiteStorage())

@app.api_route("/files", methods=["OPTIONS", "POST"])
@app.api_route("/files/{upload_id}", methods=["HEAD", "PATCH", "DELETE"])
async def handle_upload(request: Request):
    body = await request.body()
    status, headers, response_body = tus_server.handle_request(
        request.method, request.url.path, dict(request.headers), body
    )
    return Response(content=response_body, status_code=status, headers=headers)
```

## Running

=== "uv"

    ```bash
    uv add fastapi uvicorn resumable-upload
    uvicorn main:app --reload
    ```

=== "pip"

    ```bash
    pip install fastapi uvicorn resumable-upload
    uvicorn main:app --reload
    ```

See `examples/server/fastapi_app.py` for a complete working example.
