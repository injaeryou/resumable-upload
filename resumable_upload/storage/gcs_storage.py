"""Google Cloud Storage backend for resumable uploads.

Uses GCS compose API to map TUS chunked uploads to GCS objects.
Metadata is stored as .info JSON blobs alongside the upload data.

Requires google-cloud-storage: pip install resumable-upload[gcs]
"""

import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from resumable_upload.storage.base import Storage

logger = logging.getLogger(__name__)

try:
    from google.cloud import storage as gcs_lib
    from google.cloud.exceptions import NotFound, PreconditionFailed
except ImportError as e:
    raise ImportError(
        "google-cloud-storage is required for GCSStorage. "
        "Install it with: pip install resumable-upload[gcs]"
    ) from e


class GCSStorage(Storage):
    """GCS-backed storage using compose for multi-part assembly.

    Each TUS upload maps to:
    - {prefix}/{upload_id}.info    — JSON metadata
    - {prefix}/{upload_id}.buffer  — buffered data not yet flushed as a part
    - {prefix}/{upload_id}.part.N  — individual flushed parts

    After complete_upload() is called, the final object lives at:
    - {prefix}/{upload_id}
    """

    # GCS compose supports up to 32 source objects per call.
    _MAX_COMPOSE_PER_CALL = 32
    _MIN_PART_SIZE = 5 * 1024 * 1024

    def __init__(
        self,
        bucket: str,
        gcs_client: Any = None,
        prefix: str = "",
        part_size: int = 8 * 1024 * 1024,
    ):
        """Initialize GCS storage.

        Args:
            bucket: GCS bucket name
            gcs_client: Pre-configured google.cloud.storage.Client (created from env if None)
            prefix: Key prefix for all objects (no trailing slash)
            part_size: Target part size for chunked uploads (default 8MB).
                Actual flush threshold is max(part_size, 5MB) to limit the number
                of part objects created (relevant for the 32-object compose limit).
        """
        self.client = gcs_client or gcs_lib.Client()
        self.gcs_bucket = self.client.bucket(bucket)
        self.bucket_name = bucket
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

    def _part_key(self, upload_id: str, part_number: int) -> str:
        parts = [self.prefix, f"{upload_id}.part.{part_number}"]
        return "/".join(p for p in parts if p)

    def _object_key(self, upload_id: str) -> str:
        parts = [self.prefix, upload_id]
        return "/".join(p for p in parts if p)

    # -- Info persistence ----------------------------------------------------

    def _read_info(self, upload_id: str) -> Optional[dict]:
        try:
            blob = self.gcs_bucket.blob(self._info_key(upload_id))
            raw = blob.download_as_bytes()
            result: dict = json.loads(raw)
            return result
        except NotFound:
            return None

    def _write_info(self, upload_id: str, info: dict) -> None:
        blob = self.gcs_bucket.blob(self._info_key(upload_id))
        blob.upload_from_string(
            json.dumps(info).encode(),
            content_type="application/json",
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
        info = {
            "upload_id": upload_id,
            "upload_length": upload_length,
            "offset": 0,
            "metadata": metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "completed": False,
            "is_partial": is_partial,
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
            "concat_partial_ids": info.get("concat_partial_ids"),
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
        # Conditional compare-and-swap via GCS object generation: write back
        # only if the object hasn't changed since we read it. A concurrent
        # writer that already advanced the offset bumps the generation, so the
        # losing if_generation_match write gets 412 and returns False.
        #
        # Scope: this guards the offset write only. write_chunk() rewrites the
        # same info object unconditionally from a snapshot taken at its start,
        # so two PATCHes racing across processes can still lose an update; a
        # LockBackend is what serializes that pair. Folding the chunk write and
        # the offset commit into a single conditional write would remove the
        # need for one.
        blob = self.gcs_bucket.get_blob(self._info_key(upload_id))
        if blob is None:
            return False
        info = json.loads(blob.download_as_bytes())
        if info["offset"] != expected_offset:
            return False
        info["offset"] = new_offset
        try:
            blob.upload_from_string(
                json.dumps(info).encode(),
                content_type="application/json",
                if_generation_match=blob.generation,
            )
        except PreconditionFailed:
            return False
        return True

    def delete_upload(self, upload_id: str) -> None:
        info = self._read_info(upload_id)

        # Delete part blobs
        if info and info.get("parts"):
            for part in info["parts"]:
                with contextlib.suppress(NotFound):
                    self.gcs_bucket.blob(self._part_key(upload_id, part["part_number"])).delete()

        # Delete info, buffer, and final object
        for key in [
            self._info_key(upload_id),
            self._buffer_key(upload_id),
            self._object_key(upload_id),
        ]:
            with contextlib.suppress(NotFound):
                self.gcs_bucket.blob(key).delete()

    def write_chunk(self, upload_id: str, offset: int, data: bytes) -> None:
        info = self._read_info(upload_id)
        if info is None:
            raise ValueError(f"Upload {upload_id} not found")

        # Read existing buffer
        existing_buffer = b""
        if info["buffer_size"] > 0:
            try:
                blob = self.gcs_bucket.blob(self._buffer_key(upload_id))
                existing_buffer = blob.download_as_bytes()
            except NotFound:
                existing_buffer = b""

        # Append new data to buffer
        combined = existing_buffer + data

        # Flush parts while we have enough data
        while len(combined) >= self._flush_size:
            part_data = combined[: self.part_size]
            combined = combined[self.part_size :]
            self._upload_part(upload_id, info, part_data)

        # Store remaining buffer
        if combined:
            blob = self.gcs_bucket.blob(self._buffer_key(upload_id))
            blob.upload_from_string(combined)
            info["buffer_size"] = len(combined)
        else:
            with contextlib.suppress(NotFound):
                self.gcs_bucket.blob(self._buffer_key(upload_id)).delete()
            info["buffer_size"] = 0

        self._write_info(upload_id, info)

    def _upload_part(self, upload_id: str, info: dict, data: bytes) -> None:
        """Upload a single part blob to GCS."""
        part_number = len(info["parts"]) + 1
        blob = self.gcs_bucket.blob(self._part_key(upload_id, part_number))
        blob.upload_from_string(data)
        info["parts"].append(
            {
                "part_number": part_number,
                "size": len(data),
            }
        )

    def _compose_blobs(self, sources: list, destination_key: str) -> None:
        """Compose source blobs into destination, handling GCS 32-object limit.

        For > 32 sources, performs iterative hierarchical composition.
        """
        if len(sources) == 0:
            return

        if len(sources) == 1:
            # Single source: copy to destination
            self.gcs_bucket.copy_blob(sources[0], self.gcs_bucket, destination_key)
            return

        if len(sources) <= self._MAX_COMPOSE_PER_CALL:
            dest = self.gcs_bucket.blob(destination_key)
            dest.compose(sources)
            return

        # Iterative hierarchical compose for > 32 sources
        all_tmp_blobs = []
        current = sources
        tmp_round = 0

        while len(current) > self._MAX_COMPOSE_PER_CALL:
            next_level = []
            for i in range(0, len(current), self._MAX_COMPOSE_PER_CALL):
                batch = current[i : i + self._MAX_COMPOSE_PER_CALL]
                if len(batch) == 1:
                    next_level.append(batch[0])
                else:
                    tmp_key = f"{destination_key}._compose_tmp.{tmp_round}.{len(next_level)}"
                    tmp_blob = self.gcs_bucket.blob(tmp_key)
                    tmp_blob.compose(batch)
                    next_level.append(tmp_blob)
                    all_tmp_blobs.append(tmp_blob)
            current = next_level
            tmp_round += 1

        # Final compose
        dest = self.gcs_bucket.blob(destination_key)
        if len(current) == 1:
            self.gcs_bucket.copy_blob(current[0], self.gcs_bucket, destination_key)
        else:
            dest.compose(current)

        # Clean up all intermediates
        for blob in all_tmp_blobs:
            with contextlib.suppress(NotFound):
                blob.delete()

    def complete_upload(self, upload_id: str) -> bool:
        """Finalize the upload into a single GCS object.

        Strategy:
        - If parts were flushed: flush remaining buffer as last part, then
          compose all parts into the final object.
        - If no parts yet (all data in buffer): upload directly as final object.
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
                blob = self.gcs_bucket.blob(self._buffer_key(upload_id))
                remaining = blob.download_as_bytes()
            except NotFound:
                raise ValueError(
                    f"Upload {upload_id}: buffer blob missing but "
                    f"buffer_size={info['buffer_size']}. "
                    "Cannot complete without buffered data."
                ) from None

        has_parts = len(info["parts"]) > 0

        if has_parts:
            # Flush remaining buffer as last part
            if remaining:
                self._upload_part(upload_id, info, remaining)

            # Compose all parts into final object
            source_blobs = [
                self.gcs_bucket.blob(self._part_key(upload_id, p["part_number"]))
                for p in info["parts"]
            ]
            self._compose_blobs(source_blobs, self._object_key(upload_id))

            # Clean up part blobs
            for blob in source_blobs:
                with contextlib.suppress(NotFound):
                    blob.delete()
        else:
            # No parts — upload buffer directly as final object
            final_blob = self.gcs_bucket.blob(self._object_key(upload_id))
            final_blob.upload_from_string(remaining)

        # Clean up buffer
        with contextlib.suppress(NotFound):
            self.gcs_bucket.blob(self._buffer_key(upload_id)).delete()

        info["completed"] = True
        info["parts"] = []
        self._write_info(upload_id, info)
        return True

    def read_file(self, upload_id: str) -> bytes:
        try:
            blob = self.gcs_bucket.blob(self._object_key(upload_id))
            data: bytes = blob.download_as_bytes()
            return data
        except NotFound:
            raise FileNotFoundError(f"Upload {upload_id} not found in GCS") from None

    def get_file_info(self, upload_id: str) -> dict[str, Any]:
        return {
            "upload_id": upload_id,
            "bucket": self.bucket_name,
            "key": self._object_key(upload_id),
        }

    def get_expired_uploads(self) -> list[str]:
        now = datetime.now(timezone.utc)
        expired = []

        prefix = f"{self.prefix}/" if self.prefix else ""
        blobs = self.client.list_blobs(self.gcs_bucket, prefix=prefix)
        for blob in blobs:
            if not blob.name.endswith(".info"):
                continue
            try:
                data = blob.download_as_bytes()
                info = json.loads(data)
                if info.get("expires_at"):
                    dt = datetime.fromisoformat(info["expires_at"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt < now:
                        expired.append(info["upload_id"])
            except (NotFound, json.JSONDecodeError, KeyError):
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
        """Merge partial uploads into a final GCS object via ``compose``.

        GCS compose accepts up to 32 source blobs per call. For larger inputs
        this method chains compose operations: every 31 sources are merged
        into the destination, then the destination is prepended to the next
        batch, and so on. This mirrors the pattern used by tusd's GCS store.
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
        sources = [self.gcs_bucket.blob(self._object_key(p["upload_id"])) for p in partials]
        destination = self.gcs_bucket.blob(self._object_key(final_id))

        compose_limit = 32
        if len(sources) <= compose_limit:
            destination.compose(sources)
        else:
            # Compose the first batch into destination, then chain.
            destination.compose(sources[:compose_limit])
            remaining = sources[compose_limit:]
            while remaining:
                batch = [destination, *remaining[: compose_limit - 1]]
                destination.compose(batch)
                remaining = remaining[compose_limit - 1 :]

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
                "concat_partial_ids": partial_ids,
                "parts": [],
                "buffer_size": 0,
            },
        )
        logger.info(
            "GCS concatenate_uploads: merged %s partials into %s (%s bytes)",
            len(partials),
            final_id,
            total_length,
        )
        return total_length
