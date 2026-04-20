#!/usr/bin/env python3
"""Flask integration example for TUS server.

Install: pip install flask
Run    : python examples/server/flask_app.py [port]

    python examples/server/flask_app.py        # default port 5000
    python examples/server/flask_app.py 9000   # custom port
"""

import logging
import sys

from flask import Flask, make_response, request

from resumable_upload import SQLiteStorage, TusServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = Flask(__name__)

storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus_server = TusServer(
    storage=storage,
    base_path="/files",
    max_size=100 * 1024 * 1024,  # 100 MB
    upload_expiry=3600,  # 1 hour
    cleanup_interval=300,  # clean up every 5 minutes
    cors_allow_origins="*",  # restrict in production
)


@app.route("/files", methods=["OPTIONS", "POST"])
@app.route("/files/<upload_id>", methods=["HEAD", "PATCH", "DELETE"])
def handle_upload(upload_id=None):
    status, resp_headers, body = tus_server.handle_request(
        request.method, request.path, dict(request.headers), request.get_data()
    )
    response = make_response(body, status)
    for key, value in resp_headers.items():
        response.headers[key] = value
    return response


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"TUS server (Flask) running on http://localhost:{port}")
    print(f"Upload endpoint: http://localhost:{port}/files")
    print("Press Ctrl+C to stop")
    app.run(host="0.0.0.0", port=port, debug=False)
