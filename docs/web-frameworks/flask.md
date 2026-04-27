# Flask

```python
from flask import Flask, request, make_response
from resumable_upload import TusServer, SQLiteStorage

app = Flask(__name__)
tus_server = TusServer(storage=SQLiteStorage())

@app.route('/files', methods=['OPTIONS', 'POST'])
@app.route('/files/<upload_id>', methods=['HEAD', 'PATCH', 'DELETE'])
def handle_upload(upload_id=None):
    status, headers, body = tus_server.handle_request(
        request.method, request.path, dict(request.headers), request.get_data()
    )
    response = make_response(body, status)
    for key, value in headers.items():
        response.headers[key] = value
    return response
```

## Running

=== "uv"

    ```bash
    uv add flask resumable-upload
    flask run
    ```

=== "pip"

    ```bash
    pip install flask resumable-upload
    flask run
    ```

See `examples/server/flask_app.py` for a complete working example.
