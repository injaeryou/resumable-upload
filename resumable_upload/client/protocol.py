"""TUS protocol-level queries (HEAD / OPTIONS) and metadata encoding.

Mixed into :class:`TusClient`. Lives in its own module so the client class
file stays focused on upload orchestration.
"""

import base64
import re
from typing import Any, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from resumable_upload.client._mixin_base import _ClientAttrs
from resumable_upload.exceptions import TusCommunicationError


class ProtocolMixin(_ClientAttrs):
    """TUS protocol queries and metadata helpers."""

    def encode_metadata(self, metadata: dict[str, str]) -> list:
        """
        Encode metadata according to TUS protocol specification.

        Args:
            metadata: Dictionary of metadata key-value pairs

        Returns:
            List of encoded metadata strings

        Raises:
            ValueError: If metadata keys contain invalid characters
        """
        encoded_list = []
        for key, value in metadata.items():
            key_str = str(key)

            # Validate key does not contain spaces or commas
            if re.search(r"^$|[\s,]+", key_str):
                raise ValueError(
                    f'Upload-metadata key "{key_str}" cannot be empty nor contain spaces or commas.'
                )

            value_bytes = value.encode(self.metadata_encoding)
            encoded_value = base64.b64encode(value_bytes).decode("ascii")
            encoded_list.append(f"{key_str} {encoded_value}")

        return encoded_list

    def get_metadata(self, upload_url: str) -> dict[str, str]:
        """Get metadata for an upload.

        Args:
            upload_url: URL of the upload

        Returns:
            Dictionary of metadata key-value pairs

        Raises:
            TusCommunicationError: If request fails or metadata cannot be parsed

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> metadata = client.get_metadata("http://localhost:8080/files/abc123")
            >>> # {"filename": "test.bin", "content-type": "application/octet-stream"}
        """
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }

        try:
            req = Request(upload_url, headers=headers, method="HEAD")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                upload_metadata = response.headers.get("Upload-Metadata")
                if not upload_metadata:
                    return {}

                # Parse metadata
                metadata = {}
                for pair in upload_metadata.split(","):
                    pair = pair.strip()
                    if " " in pair:
                        key, value = pair.split(" ", 1)
                        # Decode base64 value
                        try:
                            decoded_value = base64.b64decode(value).decode(self.metadata_encoding)
                            metadata[key] = decoded_value
                        except (ValueError, UnicodeDecodeError):
                            # If decoding fails, use raw value
                            metadata[key] = value

                return metadata
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(
                f"Failed to get metadata: {str(e)}",
            ) from e

    def get_upload_info(self, upload_url: str) -> dict[str, Any]:
        """Get upload information including offset, length, and metadata.

        Args:
            upload_url: URL of the upload

        Returns:
            Dictionary containing:
                - offset (int): Current upload offset in bytes
                - length (int): Total upload length in bytes
                - complete (bool): Whether upload is complete
                - metadata (dict): Upload metadata

        Raises:
            TusCommunicationError: If request fails

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> info = client.get_upload_info("http://localhost:8080/files/abc123")
            >>> print(f"Progress: {info['offset']}/{info['length']}")
            >>> print(f"Complete: {info['complete']}")
        """
        headers = {
            "Tus-Resumable": self.TUS_VERSION,
            **self.headers,
        }

        try:
            req = Request(upload_url, headers=headers, method="HEAD")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                offset_str = response.headers.get("Upload-Offset")
                length_str = response.headers.get("Upload-Length")

                offset = int(offset_str) if offset_str else 0
                length = int(length_str) if length_str else 0
                complete = length > 0 and offset >= length

                # Get metadata
                metadata = {}
                upload_metadata = response.headers.get("Upload-Metadata")
                if upload_metadata:
                    for pair in upload_metadata.split(","):
                        pair = pair.strip()
                        if " " in pair:
                            key, value = pair.split(" ", 1)
                            try:
                                decoded_value = base64.b64decode(value).decode(
                                    self.metadata_encoding
                                )
                                metadata[key] = decoded_value
                            except (ValueError, UnicodeDecodeError):
                                metadata[key] = value

                return {
                    "offset": offset,
                    "length": length,
                    "complete": complete,
                    "metadata": metadata,
                }
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(
                f"Failed to get upload info: {str(e)}",
            ) from e

    def get_server_info(self) -> dict[str, Union[str, list[str], Optional[int]]]:
        """Get server information and capabilities via OPTIONS request.

        Returns:
            Dictionary containing:
                - version (str): TUS protocol version supported by server
                - extensions (list[str]): List of supported TUS extensions
                - max_size (int | None): Maximum upload size in bytes (None if unlimited)

        Raises:
            TusCommunicationError: If request fails

        Example:
            >>> client = TusClient("http://localhost:8080/files")
            >>> info = client.get_server_info()
            >>> print(f"TUS Version: {info['version']}")
            >>> print(f"Extensions: {info['extensions']}")
            >>> print(f"Max Size: {info['max_size']}")
        """
        try:
            req = Request(self.url, method="OPTIONS")
            with urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                tus_version = response.headers.get("Tus-Version", self.TUS_VERSION)
                tus_extension = response.headers.get("Tus-Extension", "")
                tus_max_size = response.headers.get("Tus-Max-Size")

                extensions = (
                    [ext.strip() for ext in tus_extension.split(",") if ext.strip()]
                    if tus_extension
                    else []
                )

                max_size = int(tus_max_size) if tus_max_size else None

                return {
                    "version": tus_version,
                    "extensions": extensions,
                    "max_size": max_size,
                }
        except (HTTPError, URLError) as e:
            raise TusCommunicationError(
                f"Failed to get server info: {str(e)}",
            ) from e
