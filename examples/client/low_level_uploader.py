#!/usr/bin/env python3
"""Fine-grained upload control using the Uploader class directly.

Usage:
    python examples/client/low_level_uploader.py <server_url> <file_path> [upload_url]

    # Start a new upload chunk-by-chunk
    python examples/client/low_level_uploader.py http://localhost:8080/files file.bin

    # Resume an existing upload at a known URL
    python examples/client/low_level_uploader.py http://localhost:8080/files file.bin \\
        http://localhost:8080/files/abc123
"""

import os
import sys
import time

from resumable_upload import TusClient, Uploader, UploadStats


def _banner(label: str, message: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n{label}: {message}\n{line}")


def _success(message: str) -> None:
    _banner("SUCCESS", message)


def _failure(message: str) -> None:
    _banner("FAILURE", message)


def progress_bar(stats: UploadStats) -> None:
    if stats.total_bytes == 0:
        return
    pct = stats.uploaded_bytes / stats.total_bytes
    filled = int(50 * pct)
    bar = "=" * filled + "-" * (50 - filled)
    print(f"\r[{bar}] {pct * 100:.1f}%  {stats.uploaded_bytes}/{stats.total_bytes} B", end="")
    if stats.uploaded_bytes == stats.total_bytes:
        print()


def main():
    if len(sys.argv) < 3:
        print("Usage: python uploader_example.py <server_url> <file_path> [upload_url]")
        sys.exit(1)

    server_url = sys.argv[1]
    file_path = sys.argv[2]
    existing_url = sys.argv[3] if len(sys.argv) > 3 else None

    if not os.path.exists(file_path):
        _failure(f"File not found: {file_path}")
        sys.exit(1)

    # ── Example 1: Manual chunk-by-chunk upload ──────────────────────────────
    print("=== Example 1: Manual chunk-by-chunk upload ===")

    client = TusClient(server_url)

    if existing_url:
        # Resume at a known URL — Uploader queries the server HEAD to get
        # the current offset, then starts uploading from there.
        print(f"Resuming existing upload: {existing_url}")
        uploader = Uploader(url=existing_url, file_path=file_path, chunk_size=512 * 1024)
    else:
        # create_uploader() creates a new upload on the server and returns
        # an Uploader positioned at offset 0.
        uploader = client.create_uploader(
            file_path,
            metadata={"filename": os.path.basename(file_path)},
            chunk_size=512 * 1024,
        )

    print(f"Starting offset : {uploader.offset}/{uploader.file_size} B")
    print(f"Already complete: {uploader.is_complete}")
    print()

    chunk_count = 0
    while uploader.upload_chunk():
        chunk_count += 1
        stats = uploader.stats
        pct = stats.progress_percent
        print(
            f"  Chunk {chunk_count:3d}: "
            f"{stats.uploaded_bytes:>10,} / {stats.total_bytes:,} B  ({pct:.1f}%)"
        )
        time.sleep(0.05)  # simulate work between chunks

    completed_url = uploader.url
    print(f"\nDone. Chunks uploaded: {chunk_count}")
    print(f"Upload URL: {completed_url}")
    uploader.close()

    # ── Example 2: Upload entire file with progress callback ─────────────────
    print("\n=== Example 2: Upload with progress callback ===")

    with client.create_uploader(file_path) as uploader:
        upload_url = uploader.upload(progress_callback=progress_bar)
        print(f"\nUpload URL: {upload_url}")

    # ── Example 3: is_complete — completed vs. new upload ────────────────────
    print("\n=== Example 3: is_complete property ===")

    # Re-attach to the upload finished in Example 2.
    # Uploader sends a HEAD request on init, so is_complete reflects the
    # actual server-side state immediately.
    with Uploader(url=completed_url, file_path=file_path) as uploader:
        print(f"Re-attached upload  → is_complete: {uploader.is_complete}")  # True

    # A brand-new upload starts at offset 0 → is_complete is False.
    new_uploader = client.create_uploader(file_path, chunk_size=512 * 1024)
    print(f"New upload (offset 0) → is_complete: {new_uploader.is_complete}")  # False

    # Upload 3 chunks and watch the offset grow.
    for i in range(3):
        has_more = new_uploader.upload_chunk()
        stats = new_uploader.stats
        print(f"  After chunk {i + 1}: {stats.uploaded_bytes:,} / {stats.total_bytes:,} B")
        if not has_more:
            break

    print(f"After 3 chunks → is_complete: {new_uploader.is_complete}")
    new_uploader.close()

    # ── Example 4: Partial upload with stop_at ───────────────────────────────
    print("\n=== Example 4: Partial upload (stop_at) ===")

    stop_bytes = 2 * 512 * 1024  # upload only first 1 MB
    uploader = client.create_uploader(file_path, chunk_size=512 * 1024)
    uploader.upload(stop_at=stop_bytes)
    print(f"Uploaded up to {uploader.offset:,} B (stop_at={stop_bytes:,})")
    print(f"is_complete: {uploader.is_complete}")
    partial_url = uploader.url
    uploader.close()

    # Resume and finish the rest
    print("Resuming to completion...")
    with Uploader(url=partial_url, file_path=file_path, chunk_size=512 * 1024) as uploader:
        uploader.upload(progress_callback=progress_bar)
        print(f"\nis_complete: {uploader.is_complete}")
        if not uploader.is_complete:
            _failure("Example 4 resume did not reach completion")
            sys.exit(1)

    _success("low_level_uploader — all 4 examples completed")


if __name__ == "__main__":
    main()
