#!/usr/bin/env python3
"""TUS client example: upload, resume, inspect, and delete."""

import json
import os
import sys

from resumable_upload import TusClient, UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed


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


def main():
    if len(sys.argv) < 3:
        print("Usage: python client_example.py <server_url> <file_path> [headers_json]")
        print("Example: python client_example.py http://localhost:8080/files file.bin")
        print(
            "         python client_example.py http://localhost:8080/files file.bin"
            ' \'{"Authorization": "Bearer token"}\''
        )
        sys.exit(1)

    server_url = sys.argv[1]
    file_path = sys.argv[2]
    extra_headers = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    # ── 1. Create client ────────────────────────────────────────────────────
    client = TusClient(
        server_url,
        chunk_size=1 * 1024 * 1024,  # 1 MB chunks
        max_retries=3,  # Retry failed chunks up to 3 times
        retry_delay=1.0,  # Start with 1 s backoff (doubles each retry)
        timeout=30.0,  # 30 s network timeout
        checksum=True,  # SHA1 checksum verification
        headers=extra_headers,
    )

    # ── 2. Server capabilities ──────────────────────────────────────────────
    print(f"Server: {server_url}")
    try:
        info = client.get_server_info()
        print(f"  TUS version  : {info['version']}")
        print(f"  Extensions   : {', '.join(info['extensions'])}")
        if info["max_size"]:
            print(f"  Max size     : {info['max_size'] // 1024 // 1024} MB")
    except TusCommunicationError as e:
        print(f"  Could not reach server: {e}")
        sys.exit(1)

    print()

    # ── 3. Upload ───────────────────────────────────────────────────────────
    print(f"Uploading: {file_path}")
    upload_url = None
    try:
        upload_url = client.upload_file(
            file_path,
            metadata={"filename": os.path.basename(file_path)},
            progress_callback=make_progress_bar,
        )
        print(f"Upload URL: {upload_url}")
    except TusUploadFailed as e:
        print(f"Upload failed: {e}")
        sys.exit(1)
    except TusCommunicationError as e:
        print(f"Communication error: {e}")
        sys.exit(1)

    print()

    # ── 4. Inspect completed upload ─────────────────────────────────────────
    print("Upload info:")
    try:
        upload_info = client.get_upload_info(upload_url)
        print(f"  Offset   : {upload_info['offset']}/{upload_info['length']} B")
        print(f"  Complete : {upload_info['complete']}")
        print(f"  Metadata : {upload_info['metadata']}")
    except TusCommunicationError as e:
        print(f"  Could not fetch info: {e}")

    print()

    # ── 5. Delete upload ────────────────────────────────────────────────────
    answer = input("Delete the upload? [y/N] ").strip().lower()
    if answer == "y":
        try:
            client.delete_upload(upload_url)
            print("Deleted.")
        except TusCommunicationError as e:
            print(f"Delete failed: {e}")


if __name__ == "__main__":
    main()
