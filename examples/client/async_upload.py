#!/usr/bin/env python3
"""TUS async client example: upload a file using AsyncTusClient.

Usage::

    pip install resumable-upload[async]
    python async_upload.py <server_url> <file_path> [headers_json]

Example::

    python async_upload.py http://localhost:8080/files myfile.bin
    python async_upload.py http://localhost:8080/files myfile.bin \
        '{"Authorization": "Bearer token"}'
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from resumable_upload import AsyncTusClient, UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed


def _banner(label: str, message: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n{label}: {message}\n{line}")


def _success(message: str) -> None:
    _banner("SUCCESS", message)


def _failure(message: str) -> None:
    _banner("FAILURE", message)


def make_progress_bar(stats: UploadStats) -> None:
    if stats.total_bytes == 0:
        return
    pct = stats.uploaded_bytes / stats.total_bytes
    filled = int(50 * pct)
    bar = "=" * filled + "-" * (50 - filled)
    speed = stats.upload_speed / 1024 / 1024
    print(
        f"\r[{bar}] {pct * 100:.1f}%  {stats.uploaded_bytes}/{stats.total_bytes} B"
        f"  {speed:.2f} MB/s",
        end="",
        flush=True,
    )
    if stats.uploaded_bytes == stats.total_bytes:
        print()


async def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python async_upload.py <server_url> <file_path> [headers_json]")
        print("Example: python async_upload.py http://localhost:8080/files file.bin")
        print(
            "         python async_upload.py http://localhost:8080/files file.bin"
            ' \'{"Authorization": "Bearer token"}\''
        )
        sys.exit(1)

    server_url = sys.argv[1]
    file_path = sys.argv[2]
    extra_headers = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    if not os.path.exists(file_path):
        _failure(f"File not found: {file_path}")
        sys.exit(1)

    # ── 1. Upload ───────────────────────────────────────────────────────────
    print(f"Server: {server_url}")
    print(f"Uploading: {file_path}")
    upload_url: str | None = None

    try:
        async with AsyncTusClient(
            server_url,
            chunk_size=1 * 1024 * 1024,
            max_retries=3,
            retry_delay=1.0,
            timeout=30.0,
            checksum=True,
            headers=extra_headers,
        ) as client:
            try:
                upload_url = await client.upload_file(
                    file_path,
                    metadata={"filename": os.path.basename(file_path)},
                    progress_callback=make_progress_bar,
                )
                print(f"Upload URL: {upload_url}")
            except TusUploadFailed as e:
                _failure(f"Upload failed: {e}")
                sys.exit(1)
            except TusCommunicationError as e:
                _failure(f"Communication error: {e}")
                sys.exit(1)

            # ── 2. Inspect completed upload ─────────────────────────────────
            print("\nUpload info:")
            upload_complete = False
            try:
                info = await client.get_upload_info(upload_url)
                upload_complete = bool(info["complete"])
                print(f"  Offset   : {info['offset']}/{info['length']} B")
                print(f"  Complete : {info['complete']}")
                print(f"  Metadata : {info['metadata']}")
            except TusCommunicationError as e:
                _failure(f"Could not fetch info: {e}")
                sys.exit(1)

    except TusCommunicationError as e:
        _failure(f"Could not reach server: {e}")
        sys.exit(1)

    if not upload_complete:
        _failure("server reported upload as incomplete")
        sys.exit(1)

    _success(f"async_upload — uploaded and inspected {file_path}")


if __name__ == "__main__":
    asyncio.run(main())
