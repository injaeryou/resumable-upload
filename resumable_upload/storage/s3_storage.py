"""S3 storage backend for resumable uploads.

Uses S3 multipart upload API to map TUS chunked uploads to S3 parts.
Metadata is stored as .info JSON objects alongside the upload data.

Requires boto3: pip install resumable-upload[s3]
"""

import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from resumable_upload.storage.base import Storage

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError as e:
    raise ImportError(
        "boto3 is required for S3Storage. Install it with: pip install resumable-upload[s3]"
    ) from e


class S3Storage(Storage):
    """S3-backed storage using multipart uploads.

    Each TUS upload maps to:
    - {prefix}/{upload_id}.info  — JSON metadata
    - {prefix}/{upload_id}.buffer — buffered data not yet flushed as an S3 part
    - S3 multipart upload — the actual file assembled from parts

    After complete_upload() is called, the final object lives at:
    - {prefix}/{upload_id}
    """

    # S3 requires each part (except the last) to be at least 5MB.
    _MIN_PART_SIZE = 5 * 1024 * 1024

    def __init__(
        self,
        bucket: str,
        s3_client: Any = None,
        prefix: str = "",
        part_size: int = 8 * 1024 * 1024,
    ):
        """Initialize S3 storage.

        Args:
            bucket: S3 bucket name
            s3_client: Pre-configured boto3 S3 client (created from env if None)
            prefix: Key prefix for all objects (no trailing slash)
            part_size: Target part size for multipart uploads (default 8MB).
                Actual flush threshold is max(part_size, 5MB) to satisfy S3 constraints.
        """
        self.bucket = bucket
        self.s3 = s3_client or boto3.client("s3")
        self.prefix = prefix.strip("/")
        self.part_size = part_size
        self._flush_size = max(part_size, self._MIN_PART_SIZE)

    # -- Key helpers ---------------------------------------------------------

    def _info_key(self, upload_id: str) -> str:
        parts = [self.prefix, f"{upload_id}.info"]
        return "/".join(p for p in parts if p)

    def _buffer_key(self, upload_id: str) -> str:
        parts = [self.prefix, f"{upload_id}.buffer"]
        return "/".join(p for p in parts if p)

    def _object_key(self, upload_id: str) -> str:
        parts = [self.prefix, upload_id]
        return "/".join(p for p in parts if p)

    # -- Info persistence ----------------------------------------------------

    def _read_info(self, upload_id: str) -> Optional[dict]:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._info_key(upload_id))
            result: dict = json.loads(resp["Body"].read())
            return result
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def _write_info(self, upload_id: str, info: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._info_key(upload_id),
            Body=json.dumps(info).encode(),
            ContentType="application/json",
        )

    # -- Storage ABC implementation ------------------------------------------

    def create_upload(
        self,
        upload_id: str,
        upload_length: Optional[int],
        metadata: dict[str, str],
        expires_at: Optional[datetime] = None,
        is_partial: bool = False,
    ) -> None:
        # Start S3 multipart upload
        mpu = self.s3.create_multipart_upload(
            Bucket=self.bucket,
            Key=self._object_key(upload_id),
        )
        multipart_upload_id = mpu["UploadId"]

        info = {
            "upload_id": upload_id,
            "upload_length": upload_length,
            "offset": 0,
            "metadata": metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "completed": False,
            "is_partial": is_partial,
            "multipart_upload_id": multipart_upload_id,
            "parts": [],
            "buffer_size": 0,
        }
        self._write_info(upload_id, info)

    def get_upload(self, upload_id: str) -> Optional[dict[str, Any]]:
        info = self._read_info(upload_id)
        if info is None:
            return None

        expires_at = None
        if info.get("expires_at"):
            try:
                dt = datetime.fromisoformat(info["expires_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                expires_at = dt
            except (ValueError, AttributeError):
                pass

        return {
            "upload_id": info["upload_id"],
            "upload_length": info["upload_length"],
            "offset": info["offset"],
            "metadata": info.get("metadata", {}),
            "completed": info.get("completed", False),
            "expires_at": expires_at,
            "is_partial": info.get("is_partial", False),
        }

    def update_offset(self, upload_id: str, offset: int) -> None:
        info = self._read_info(upload_id)
        if info is None:
            return
        info["offset"] = offset
        self._write_info(upload_id, info)

    def set_upload_length(self, upload_id: str, upload_length: int) -> None:
        info = self._read_info(upload_id)
        if info is None:
            return
        info["upload_length"] = upload_length
        self._write_info(upload_id, info)

    def update_offset_atomic(self, upload_id: str, expected_offset: int, new_offset: int) -> bool:
        info = self._read_info(upload_id)
        if info is None or info["offset"] != expected_offset:
            return False
        info["offset"] = new_offset
        self._write_info(upload_id, info)
        return True

    def delete_upload(self, upload_id: str) -> None:
        info = self._read_info(upload_id)

        # Abort multipart upload if still in progress
        if info and info.get("multipart_upload_id") and not info.get("completed"):
            try:
                self.s3.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=self._object_key(upload_id),
                    UploadId=info["multipart_upload_id"],
                )
            except ClientError:
                logger.debug("Failed to abort multipart upload for %s", upload_id)

        # Delete all related objects
        for key in [
            self._info_key(upload_id),
            self._buffer_key(upload_id),
            self._object_key(upload_id),
        ]:
            with contextlib.suppress(ClientError):
                self.s3.delete_object(Bucket=self.bucket, Key=key)

    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        info = self._read_info(upload_id)
        if info is None:
            raise ValueError(f"Upload {upload_id} not found")

        # Read existing buffer
        existing_buffer = b""
        if info["buffer_size"] > 0:
            try:
                resp = self.s3.get_object(Bucket=self.bucket, Key=self._buffer_key(upload_id))
                existing_buffer = resp["Body"].read()
            except ClientError:
                existing_buffer = b""

        # Append new data to buffer
        combined = existing_buffer + data

        # Flush parts while we have enough data (respecting S3 min part size)
        while len(combined) >= self._flush_size:
            part_data = combined[: self.part_size]
            combined = combined[self.part_size :]
            self._upload_part(upload_id, info, part_data)

        # Store remaining buffer
        if combined:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self._buffer_key(upload_id),
                Body=combined,
            )
            info["buffer_size"] = len(combined)
        else:
            # Clear buffer
            with contextlib.suppress(ClientError):
                self.s3.delete_object(Bucket=self.bucket, Key=self._buffer_key(upload_id))
            info["buffer_size"] = 0

        self._write_info(upload_id, info)

    def _upload_part(self, upload_id: str, info: dict, data: bytes) -> None:
        """Upload a single part to the S3 multipart upload."""
        part_number = len(info["parts"]) + 1
        resp = self.s3.upload_part(
            Bucket=self.bucket,
            Key=self._object_key(upload_id),
            UploadId=info["multipart_upload_id"],
            PartNumber=part_number,
            Body=data,
        )
        info["parts"].append(
            {
                "PartNumber": part_number,
                "ETag": resp["ETag"],
                "Size": len(data),
            }
        )

    def complete_upload(self, upload_id: str) -> bool:
        """Finalize the upload into a single S3 object.

        Strategy:
        - If parts were flushed via multipart: flush remaining buffer as last
          part (S3 allows < 5MB for the final part), then CompleteMultipartUpload.
        - If no parts yet (all data in buffer): abort multipart and use a single
          PutObject. This avoids S3's 5MB minimum part size constraint for small files.
        """
        info = self._read_info(upload_id)
        if info is None:
            raise ValueError(f"Upload {upload_id} not found")
        if info.get("completed"):
            return False

        # Read remaining buffer
        remaining = b""
        if info["buffer_size"] > 0:
            try:
                resp = self.s3.get_object(Bucket=self.bucket, Key=self._buffer_key(upload_id))
                remaining = resp["Body"].read()
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    raise ValueError(
                        f"Upload {upload_id}: buffer object missing but "
                        f"buffer_size={info['buffer_size']}. "
                        "Cannot complete without buffered data."
                    ) from None
                raise

        has_parts = len(info["parts"]) > 0

        if has_parts:
            # Flush remaining buffer as last part (allowed to be < 5MB)
            if remaining:
                self._upload_part(upload_id, info, remaining)

            self.s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=self._object_key(upload_id),
                UploadId=info["multipart_upload_id"],
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in info["parts"]
                    ]
                },
            )
        else:
            # No parts flushed — abort multipart, use single PutObject
            with contextlib.suppress(ClientError):
                self.s3.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=self._object_key(upload_id),
                    UploadId=info["multipart_upload_id"],
                )
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self._object_key(upload_id),
                Body=remaining,
            )

        # Clean up buffer object
        with contextlib.suppress(ClientError):
            self.s3.delete_object(Bucket=self.bucket, Key=self._buffer_key(upload_id))

        info["completed"] = True
        info["multipart_upload_id"] = None
        self._write_info(upload_id, info)
        return True

    def read_file(self, upload_id: str) -> bytes:
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=self._object_key(upload_id))
            data: bytes = resp["Body"].read()
            return data
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise FileNotFoundError(f"Upload {upload_id} not found in S3") from e
            raise

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        return {
            "upload_id": upload_id,
            "bucket": self.bucket,
            "key": self._object_key(upload_id),
        }

    def get_expired_uploads(self) -> list[str]:
        now = datetime.now(timezone.utc)
        expired = []

        # List all .info objects under prefix
        prefix = f"{self.prefix}/" if self.prefix else ""
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".info"):
                    continue
                try:
                    resp = self.s3.get_object(Bucket=self.bucket, Key=key)
                    info = json.loads(resp["Body"].read())
                    if info.get("expires_at"):
                        dt = datetime.fromisoformat(info["expires_at"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < now:
                            expired.append(info["upload_id"])
                except (ClientError, json.JSONDecodeError, KeyError):
                    continue

        return expired

    def cleanup_expired_uploads(self) -> int:
        expired_ids = self.get_expired_uploads()
        for upload_id in expired_ids:
            self.delete_upload(upload_id)
        return len(expired_ids)

    def concatenate_uploads(
        self,
        final_id: str,
        partial_ids: list[str],
        metadata: dict[str, str],
        *,
        expires_at: Optional[datetime] = None,
    ) -> int:
        """Merge partial uploads into a final S3 object via UploadPartCopy.

        S3 requires every non-last part to be at least 5 MiB. Partials smaller
        than that must be the last in the list, otherwise S3 will reject the
        CompleteMultipartUpload call. Callers that need small-partial
        concatenation should merge client-side and upload the result as a
        single object.
        """
        partials = []
        for pid in partial_ids:
            p = self.get_upload(pid)
            if p is None:
                raise ValueError(f"partial upload not found: {pid}")
            if not p.get("is_partial"):
                raise ValueError(f"upload {pid} is not a partial upload")
            if p["offset"] != p["upload_length"]:
                raise ValueError(f"partial upload {pid} is not complete")
            partials.append(p)

        total_length = sum(p["upload_length"] for p in partials)
        final_key = self._object_key(final_id)

        mpu = self.s3.create_multipart_upload(Bucket=self.bucket, Key=final_key)
        multipart_upload_id = mpu["UploadId"]

        try:
            parts = []
            for idx, p in enumerate(partials, start=1):
                src_key = self._object_key(p["upload_id"])
                copy = self.s3.upload_part_copy(
                    Bucket=self.bucket,
                    Key=final_key,
                    PartNumber=idx,
                    UploadId=multipart_upload_id,
                    CopySource={"Bucket": self.bucket, "Key": src_key},
                )
                parts.append(
                    {
                        "ETag": copy["CopyPartResult"]["ETag"],
                        "PartNumber": idx,
                    }
                )

            self.s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=final_key,
                UploadId=multipart_upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            with contextlib.suppress(ClientError):
                self.s3.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=final_key,
                    UploadId=multipart_upload_id,
                )
            raise

        self._write_info(
            final_id,
            {
                "upload_id": final_id,
                "upload_length": total_length,
                "offset": total_length,
                "metadata": metadata,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "completed": True,
                "is_partial": False,
                "multipart_upload_id": None,
                "parts": [],
                "buffer_size": 0,
            },
        )
        logger.info(
            "S3 concatenate_uploads: merged %s partials into %s (%s bytes)",
            len(partials),
            final_id,
            total_length,
        )
        return total_length
