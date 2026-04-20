#!/usr/bin/env python3
"""Django integration example for TUS server.

Install: pip install django
Run    : python examples/server/django_app.py [port]

    python examples/server/django_app.py        # default port 8000
    python examples/server/django_app.py 9000   # custom port

To integrate into an existing Django project:
  1. Copy tus_upload_view() into your views.py
  2. Wire up URLs as shown in the urlpatterns block below
"""

import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from resumable_upload import SQLiteStorage, TusServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Create once at module level (or inject via Django app config / settings)
storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus_server = TusServer(
    storage=storage,
    base_path="/files",
    max_size=100 * 1024 * 1024,  # 100 MB
    upload_expiry=3600,  # 1 hour
    cleanup_interval=300,  # clean up every 5 minutes
    cors_allow_origins="*",  # restrict in production
)


@csrf_exempt
@require_http_methods(["OPTIONS", "POST", "HEAD", "PATCH", "DELETE"])
def tus_upload_view(request, upload_id=None):
    """Handle TUS upload requests.

    urls.py:
        from .views import tus_upload_view
        urlpatterns = [
            path("files", tus_upload_view, name="tus-create"),
            path("files/<str:upload_id>", tus_upload_view, name="tus-upload"),
        ]
    """
    # Django stores HTTP headers as HTTP_<HEADER> in META; normalise to Header-Name form.
    headers = {
        key[5:].replace("_", "-"): value
        for key, value in request.META.items()
        if key.startswith("HTTP_")
    }
    if request.META.get("CONTENT_TYPE"):
        headers["Content-Type"] = request.META["CONTENT_TYPE"]
    if request.META.get("CONTENT_LENGTH"):
        headers["Content-Length"] = request.META["CONTENT_LENGTH"]

    status, resp_headers, body = tus_server.handle_request(
        request.method, request.path, headers, request.body
    )

    response = HttpResponse(body, status=status)
    for key, value in resp_headers.items():
        response[key] = value
    return response


# ── Standalone runner ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    import django
    from django.conf import settings
    from django.core.servers.basehttp import WSGIRequestHandler, WSGIServer
    from django.core.wsgi import get_wsgi_application
    from django.urls import path

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    settings.configure(
        DEBUG=True,
        SECRET_KEY="django-insecure-example-key",
        ROOT_URLCONF=__name__,
        ALLOWED_HOSTS=["*"],
        MIDDLEWARE=["django.middleware.common.CommonMiddleware"],
    )
    django.setup()

    urlpatterns = [
        path("files", tus_upload_view),
        path("files/<str:upload_id>", tus_upload_view),
    ]

    print(f"TUS server (Django) running on http://localhost:{port}")
    print(f"Upload endpoint: http://localhost:{port}/files")
    print("Press Ctrl+C to stop")

    application = get_wsgi_application()
    server = WSGIServer(("0.0.0.0", port), WSGIRequestHandler)
    server.set_app(application)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
