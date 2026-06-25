# ASGI (FastAPI / Starlette / Quart / …)

`TusASGIApp` is a generic ASGI adapter that awaits `TusServer.handle_request_async`. Sync-only `Storage` backends inherit `asyncio.to_thread`-based defaults on the `Storage` ABC, so the event loop stays free with no async rewrite required. True-async backends (see [Storage › Async-native backends](../api-reference/storage.md#async-native-backends)) plug in by overriding the specific `*_async` methods they have non-blocking implementations for.

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

1. Drains the request body off the ASGI receive channel into a single `bytes` buffer (the handler does not stream chunks).
2. Decodes scope headers from latin-1 byte tuples into a string dict.
3. `await`s `TusServer.handle_request_async(method, path, headers, body)`. Every storage call along the request path goes through the `Storage._async` surface, so override-aware backends stay non-blocking end-to-end and sync-only backends fall back to a single `asyncio.to_thread` hop per call.
4. Streams the response back as one `http.response.start` + one `http.response.body` message.

`lifespan` events are accepted and acknowledged but otherwise ignored — there's no startup or shutdown work to perform. Non-HTTP scopes raise `NotImplementedError`.

## Examples

- `examples/server/asgi_app.py` — `TusASGIApp` served directly by uvicorn (async dispatch over the default `SQLiteStorage`).
- `examples/server/async_storage.py` — a native-async `Storage` backend (`AsyncDictStorage`) that awaits real non-blocking I/O end-to-end, served over `TusASGIApp`. The template to copy for `aiofiles` / `aioboto3` / `asyncpg` backends.
- `examples/server/fastapi_app.py` — thin FastAPI route wrapper (calls the sync `handle_request`).

## Async client

For an async *client* that pairs with the async server above, see
[AsyncTusClient](../api-reference/client.md#async-client) and
`examples/client/async_upload.py`. It uses `httpx` under the hood and is
installed via `pip install "resumable-upload[async]"`.
