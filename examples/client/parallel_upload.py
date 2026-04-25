"""Parallel chunk upload + manual partial/final primitives.

Two demonstrations:

1. ``parallel_uploads=N`` — one call, the client splits the file into N
   byte ranges, uploads them concurrently, and merges server-side via the
   TUS concatenation extension. Matches tus-js-client's ``parallelUploads``.

2. Manual partial/final — upload parts one at a time (e.g. from different
   devices or sessions) and stitch them together explicitly.

Both require a server that supports the ``concatenation`` extension;
this library does.

Run::

    # Start a server first
    python examples/server/http_server.py 8080 &

    # Automatic parallel upload (4 concurrent partials, merged server-side)
    python examples/client/parallel_upload.py http://localhost:8080/files /path/to/big.bin

    # Manual partial/final flow (the example also writes 2 tiny temp files)
    python examples/client/parallel_upload.py --manual http://localhost:8080/files
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from resumable_upload import TusClient, UploadStats


def _banner(label: str, message: str) -> None:
    line = "=" * 60
    print(f"\n{line}\n{label}: {message}\n{line}")


def _success(message: str) -> None:
    _banner("SUCCESS", message)


def _failure(message: str) -> None:
    _banner("FAILURE", message)


def progress(stats: UploadStats) -> None:
    print(f"  {stats.progress_percent:5.1f}% {stats.uploaded_bytes}/{stats.total_bytes} bytes")


def demo_parallel(base_url: str, file_path: str, n: int = 4) -> None:
    client = TusClient(base_url, chunk_size=1024 * 1024)
    print(f"Uploading {file_path} with parallel_uploads={n}")
    url = client.upload_file(
        file_path,
        metadata={"filename": Path(file_path).name},
        parallel_uploads=n,
        progress_callback=progress,
    )
    info = client.get_upload_info(url)
    print(
        f"\nmerged final upload at {url}\n  "
        f"length={info['length']} bytes  complete={info['complete']}"
    )
    if not info["complete"]:
        _failure(f"parallel_upload — server reports merged upload as incomplete: {url}")
        sys.exit(1)
    _success(f"parallel_upload — {n} partials merged into {url}")


def demo_manual(base_url: str) -> None:
    """Show raw partial/final primitives with two tiny in-memory parts."""
    client = TusClient(base_url, chunk_size=1024)

    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a.bin"
        b = Path(tmp) / "b.bin"
        a.write_bytes(b"hello-")
        b.write_bytes(b"world")
        expected_length = a.stat().st_size + b.stat().st_size

        print("Creating partial A ...")
        url_a = client.create_partial_upload(str(a))
        print(f"  {url_a}")

        print("Creating partial B ...")
        url_b = client.create_partial_upload(str(b))
        print(f"  {url_b}")

        print("Merging into final upload ...")
        final_url = client.create_final_upload(
            partial_urls=[url_a, url_b],
            metadata={"filename": "merged.bin"},
        )
        info = client.get_upload_info(final_url)
        print(f"\nfinal at {final_url}\n  length={info['length']}  complete={info['complete']}")
        if not info["complete"] or info["length"] != expected_length:
            _failure(
                f"manual partial/final — expected length={expected_length} complete=True, "
                f"got length={info['length']} complete={info['complete']}"
            )
            sys.exit(1)
        _success(f"manual partial/final — merged {expected_length} bytes at {final_url}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--manual":
        if len(args) < 2:
            sys.exit("usage: parallel_upload.py --manual <server_url>")
        demo_manual(args[1])
    else:
        if len(args) < 2:
            sys.exit(
                "usage: parallel_upload.py <server_url> <file_path> [n_parallel]\n"
                "       parallel_upload.py --manual <server_url>"
            )
        base_url, file_path = args[0], args[1]
        n = int(args[2]) if len(args) > 2 else 4
        demo_parallel(base_url, file_path, n)


if __name__ == "__main__":
    main()
