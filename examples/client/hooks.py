"""Client hooks + previous-upload discovery.

Demonstrates:

- ``before_request(method, url, headers)`` — observe every HTTP call
- ``after_response(method, url, status)`` — observe every successful response
- ``on_should_retry(err, attempt)`` — veto specific retries (e.g. never
  retry on auth failure)
- ``find_previous_uploads(file_path)`` — look up a resumable upload URL
  previously saved to URL storage, matching tus-js-client's
  ``findPreviousUploads`` semantics
- ``SQLiteURLStorage`` — durable, multi-process-safe URL persistence

Run::

    python examples/server/http_server.py 8080 &
    python examples/client/hooks.py http://localhost:8080/files /path/to/file.bin
    # Run a second time to see find_previous_uploads resume the same URL
"""

from __future__ import annotations

import sys
from pathlib import Path

from resumable_upload import SQLiteURLStorage, TusClient
from resumable_upload.exceptions import TusUploadFailed


def _banner(label: str, message: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n{label}: {message}\n{line}")


def _success(message: str) -> None:
    _banner("SUCCESS", message)


def _failure(message: str) -> None:
    _banner("FAILURE", message)


def log_before(method: str, url: str, headers: dict[str, str]) -> None:
    print(f"→ {method} {url}")


def log_after(method: str, url: str, status: int) -> None:
    arrow = "✓" if 200 <= status < 300 else "✗"
    print(f"{arrow} {method} {url} → {status}")


def should_retry(err: Exception, attempt: int) -> bool:
    """Never retry on TusUploadFailed caused by a 4xx (permanent error)."""
    msg = str(err)
    if "401" in msg or "403" in msg or "404" in msg:
        print(f"  (auth/not-found — not retrying, attempt={attempt})")
        return False
    return True


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: hooks.py <server_url> <file_path>")
    base_url, file_path = sys.argv[1], sys.argv[2]

    url_storage = SQLiteURLStorage(db_path=".hooks_example_urls.db")
    client = TusClient(
        base_url,
        chunk_size=64 * 1024,
        store_url=True,
        url_storage=url_storage,
        max_retries=5,
        retry_delay=0.5,
        before_request=log_before,
        after_response=log_after,
        on_should_retry=should_retry,
    )

    # Previous-upload discovery (run this example twice to see it resume).
    previous = client.find_previous_uploads(file_path)
    if previous:
        url = previous[0]["upload_url"]
        print(f"Resuming previous upload at {url}\n")
        try:
            client.resume_upload(file_path, url)
        except TusUploadFailed as e:
            # Stored URL may have expired / been terminated server-side.
            print(f"resume failed ({e}); starting fresh")
            url_storage.remove_url(previous[0]["fingerprint"])
            url = client.upload_file(file_path, metadata={"filename": Path(file_path).name})
    else:
        print("No previous upload found; starting fresh\n")
        url = client.upload_file(file_path, metadata={"filename": Path(file_path).name})

    info = client.get_upload_info(url)
    print(f"\nfinal at {url}  ({info['offset']}/{info['length']} bytes)")
    if not info["complete"]:
        _failure(f"hooks — server reports upload as incomplete: {url}")
        sys.exit(1)
    _success(f"hooks — uploaded {info['length']} bytes via {url}")


if __name__ == "__main__":
    main()
