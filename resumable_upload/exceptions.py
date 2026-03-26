"""
Global resumable_upload exception classes.

Based on tusclient exceptions for compatibility with TUS protocol error handling.
"""


class TusCommunicationError(Exception):
    """
    Exception raised when communication with TUS server behaves unexpectedly.

    Attributes:
        message (str): Main message of the exception
        status_code (int): HTTP status code of response indicating an error
        response_content (bytes): Content of response indicating an error
    """

    def __init__(self, message, status_code=None, response_content=None):
        default_message = f"Communication with TUS server failed with status {status_code}"
        message = message or default_message
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_content = response_content


class TusUploadFailed(TusCommunicationError):
    """Exception raised when an attempted upload fails."""

    pass


class TusHookError(Exception):
    """Exception raised in server hooks to reject a request with a specific HTTP status.

    Pre-hooks (on_incoming_request, on_upload_create) can raise this to abort
    the request and return the given status code and message to the client.

    Attributes:
        status_code: HTTP status code to return (default: 403)
        body: Response body string
    """

    def __init__(self, body: str = "Forbidden", status_code: int = 403):
        super().__init__(body)
        self.status_code = status_code
        self.body = body
