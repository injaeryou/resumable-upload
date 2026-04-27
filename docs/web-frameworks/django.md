# Django

```python
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from resumable_upload import TusServer, SQLiteStorage

tus_server = TusServer(storage=SQLiteStorage())

@csrf_exempt
def tus_upload_view(request, upload_id=None):
    headers = {key[5:].replace('_', '-'): value
               for key, value in request.META.items() if key.startswith('HTTP_')}
    status, response_headers, response_body = tus_server.handle_request(
        request.method, request.path, headers, request.body
    )
    response = HttpResponse(response_body, status=status)
    for key, value in response_headers.items():
        response[key] = value
    return response
```

## URL Configuration

```python
# urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('files/', views.tus_upload_view),
    path('files/<str:upload_id>/', views.tus_upload_view),
]
```

## Running

=== "uv"

    ```bash
    uv add django resumable-upload
    python manage.py runserver
    ```

=== "pip"

    ```bash
    pip install django resumable-upload
    python manage.py runserver
    ```

See `examples/server/django_app.py` for a complete working example.
