# ASGI (FastAPI / Starlette / Quart / …)

`TusASGIApp` is a generic ASGI adapter that wraps a synchronous `TusServer` and runs `handle_request` on a thread pool via `asyncio.to_thread`. The event loop stays free, no async storage rewrite required.

## Mount on FastAPI

```python
from fastapi import FastAPI
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

app = FastAPI()
tus = TusServer(storage=SQLiteStorage(), base_path="/files")
app.mount("/files", TusASGIApp(tus))
```

That's the entire integration. Mount on any `path` you like — `TusASGIApp` derives the request path from the ASGI scope, so the mount point doesn't have to match `base_path`.

## Mount on Starlette

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

tus = TusServer(storage=SQLiteStorage(), base_path="/files")

app = Starlette(routes=[Mount("/files", app=TusASGIApp(tus))])
```

## What the adapter does

1. Drains the request body off the ASGI receive channel into a single `bytes` buffer (the sync handler does not stream).
2. Decodes scope headers from latin-1 byte tuples into a string dict.
3. Hands `(method, path, headers, body)` to `TusServer.handle_request` on a worker thread.
4. Streams the response back as one `http.response.start` + one `http.response.body` message.

`lifespan` events are accepted and acknowledged but otherwise ignored — there's no startup or shutdown work to perform. Non-HTTP scopes raise `NotImplementedError`.

## Examples

- `examples/server/asgi_app.py` — minimal Starlette mount.
- `examples/server/fastapi_app.py` — full FastAPI integration.
