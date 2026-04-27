# Observability & Custom Retry Gating

`TusClient` exposes three lightweight hooks for tracing requests and shaping retry behaviour. They cost nothing when unset.

## `before_request` and `after_response`

```python
def before(method, url, headers):
    print(f"-> {method} {url}")

def after(method, url, status):
    print(f"<- {method} {url} {status}")

client = TusClient(
    "http://localhost:8080/files",
    before_request=before,
    after_response=after,
)
```

`before_request` fires before each HTTP request the client issues (POST, HEAD, PATCH, DELETE). `after_response` fires after each response, with the final status code. They run on the calling thread, synchronously — keep them cheap (logging, metrics, span creation) and avoid blocking I/O.

The hooks are forwarded to any `Uploader` you obtain from `TusClient.create_uploader()`, so chunk PATCHes are observed too.

## `on_should_retry`

```python
def should_retry(err: Exception, attempt: int) -> bool:
    if isinstance(err, PermissionError):
        return False              # don't retry auth failures
    return attempt < 5            # cap above the configured max_retries

client = TusClient(
    "http://localhost:8080/files",
    max_retries=10,
    on_should_retry=should_retry,
)
```

`on_should_retry` is consulted before every retry attempt during a chunk upload. Returning `False` aborts retry immediately and surfaces a `TusUploadFailed`. Returning `True` falls through to the standard exponential-backoff schedule.

The hook receives the original exception and the 1-indexed attempt number, so it can apply domain rules ("don't retry quota errors", "give the network exactly one extra try") without touching the library's retry math.

## Examples

- `examples/client/hooks.py` — full hook set wired into a TusClient.
