#!/usr/bin/env python3
"""Cross-session resumable upload example.

Demonstrates how uploads survive process restarts by persisting the upload
URL in a local JSON file keyed by file fingerprint.

Usage:
    # First run — starts upload, then interrupts halfway
    python examples/client/resume.py http://localhost:8080/files large_file.bin

    # Second run — detects the stored URL and resumes from where it stopped
    python examples/client/resume.py http://localhost:8080/files large_file.bin
"""

import os
import sys

from resumable_upload import TusClient, UploadStats
from resumable_upload.exceptions import TusCommunicationError, TusUploadFailed
from resumable_upload.url_storage import FileURLStorage


def progress_bar(stats: UploadStats) -> None:
    if stats.total_bytes == 0:
        return
    pct = stats.uploaded_bytes / stats.total_bytes
    filled = int(40 * pct)
    bar = "=" * filled + "-" * (40 - filled)
    speed = stats.upload_speed / 1024 / 1024
    chunks = f"{stats.chunks_completed} chunks"
    if stats.chunks_retried:
        chunks += f" ({stats.chunks_retried} retried)"
    print(f"\r[{bar}] {pct * 100:5.1f}%  {speed:.2f} MB/s  {chunks}", end="", flush=True)
    if stats.uploaded_bytes == stats.total_bytes:
        print()


def main():
    if len(sys.argv) < 3:
        print("Usage: python resume_example.py <server_url> <file_path>")
        print("Example: python resume_example.py http://localhost:8080/files large.bin")
        sys.exit(1)

    server_url = sys.argv[1]
    file_path = sys.argv[2]

    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    # FileURLStorage persists { fingerprint → upload_url } between runs.
    # The fingerprint is a SHA-256 of the full file content + size, so
    # different files never collide.
    url_storage = FileURLStorage(".tus_urls.json")

    client = TusClient(
        server_url,
        chunk_size=512 * 1024,  # 512 KB — small chunks make resume more visible
        max_retries=3,
        retry_delay=1.0,
        store_url=True,  # enable cross-session resumability
        url_storage=url_storage,
    )

    file_size = os.path.getsize(file_path)
    print(f"File : {file_path}  ({file_size / 1024 / 1024:.1f} MB)")
    print("Store: .tus_urls.json")
    print()

    try:
        upload_url = client.upload_file(
            file_path,
            metadata={"filename": os.path.basename(file_path)},
            progress_callback=progress_bar,
        )
        print(f"Upload complete: {upload_url}")
        print()
        print("Run this script again with the same file — it will detect the")
        print("completed upload via the stored URL and skip re-uploading.")

    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        print("The partial upload URL has been saved to .tus_urls.json.")
        print("Run this script again to resume from where it stopped.")

    except TusUploadFailed as e:
        print(f"\nUpload failed: {e}")
        sys.exit(1)

    except TusCommunicationError as e:
        print(f"\nCommunication error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
