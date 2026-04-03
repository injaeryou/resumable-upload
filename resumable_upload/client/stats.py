"""Upload statistics tracking for TUS client."""

import time
from dataclasses import dataclass


@dataclass
class UploadStats:
    """Statistics for upload progress.

    Attributes:
        total_bytes: Total number of bytes to upload
        uploaded_bytes: Number of bytes uploaded so far
        chunks_completed: Number of chunks successfully uploaded
        chunks_failed: Number of chunks that failed
        chunks_retried: Number of chunks that required retries
        start_time: Timestamp when upload started
    """

    total_bytes: int
    uploaded_bytes: int = 0
    chunks_completed: int = 0
    chunks_failed: int = 0
    chunks_retried: int = 0
    start_time: float = 0.0

    def __post_init__(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @property
    def elapsed_time(self) -> float:
        """Get elapsed time in seconds."""
        return time.time() - self.start_time

    @property
    def upload_speed(self) -> float:
        """Get upload speed in bytes/second."""
        if self.elapsed_time > 0:
            return self.uploaded_bytes / self.elapsed_time
        return 0.0

    @property
    def upload_speed_mbps(self) -> float:
        """Get upload speed in MB/second."""
        return self.upload_speed / (1024 * 1024)

    @property
    def progress_percent(self) -> float:
        """Get progress as percentage (0-100)."""
        if self.total_bytes > 0:
            return (self.uploaded_bytes / self.total_bytes) * 100
        return 0.0

    @property
    def eta_seconds(self) -> float:
        """Get estimated time to completion in seconds.

        Returns 0.0 when upload is complete, float('inf') when speed is
        unknown or stalled.
        """
        if self.uploaded_bytes >= self.total_bytes:
            return 0.0
        if self.upload_speed > 0:
            remaining_bytes = self.total_bytes - self.uploaded_bytes
            return remaining_bytes / self.upload_speed
        return float("inf")

    @property
    def total_chunks(self) -> int:
        """Get total number of chunks (completed + failed)."""
        return self.chunks_completed + self.chunks_failed
