#!/usr/bin/env python3
"""Example TUS server using Python's built-in http.server.

Usage:
    python examples/server/http_server.py [port]

    python examples/server/http_server.py        # default port 8080
    python examples/server/http_server.py 9000   # custom port
"""

import logging
import sys
from http.server import HTTPServer

from resumable_upload import TusHTTPRequestHandler, TusServer
from resumable_upload.storage import SQLiteStorage


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")

    tus_server = TusServer(
        storage=storage,
        base_path="/files",
        max_size=100 * 1024 * 1024,  # 100 MB upload limit
        upload_expiry=3600,  # Uploads expire after 1 hour
        cleanup_interval=300,  # Clean up expired uploads every 5 minutes
        cors_allow_origins="*",  # Allow all origins (restrict in production)
    )

    class Handler(TusHTTPRequestHandler):
        pass

    Handler.tus_server = tus_server

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"TUS server running on http://localhost:{port}")
    print(f"Upload endpoint: http://localhost:{port}/files")
    print("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
