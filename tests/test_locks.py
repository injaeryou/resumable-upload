"""Contract tests for lock backends.

Applied to every LockBackend implementation so they agree on the
acquire/release/TTL/token-mismatch contract.
"""

from __future__ import annotations

import threading
import time

import pytest

from resumable_upload.locks import InMemoryLockBackend

_IMPLS: list[str] = ["memory"]

try:
    import fakeredis  # noqa: F401

    from resumable_upload.locks_redis import RedisLockBackend  # noqa: F401

    _IMPLS.append("redis")
except ImportError:
    pass


def _make(impl: str):
    if impl == "redis":
        import fakeredis

        from resumable_upload.locks_redis import RedisLockBackend

        return RedisLockBackend(client=fakeredis.FakeRedis())
    return InMemoryLockBackend()


@pytest.fixture(params=_IMPLS)
def lock(request):
    return _make(request.param)


class TestLockBackendContract:
    def test_acquire_then_release(self, lock):
        token = lock.acquire("upload-123", ttl_seconds=5)
        assert token is not None
        lock.release("upload-123", token)
        # Can re-acquire after release
        token2 = lock.acquire("upload-123", ttl_seconds=5)
        assert token2 is not None

    def test_second_acquire_blocks(self, lock):
        assert lock.acquire("u-1", ttl_seconds=5) is not None
        assert lock.acquire("u-1", ttl_seconds=5, wait_timeout=0.1) is None

    def test_ttl_expiry_releases_lock(self, lock):
        assert lock.acquire("u-ttl", ttl_seconds=1) is not None
        time.sleep(1.2)
        # After TTL, another acquirer succeeds
        assert lock.acquire("u-ttl", ttl_seconds=1, wait_timeout=0.5) is not None

    def test_release_with_wrong_token_is_noop(self, lock):
        token = lock.acquire("u-xyz", ttl_seconds=5)
        assert token is not None
        lock.release("u-xyz", token="not-the-real-token")
        # Original holder still holds the lock
        assert lock.acquire("u-xyz", ttl_seconds=5, wait_timeout=0.1) is None
        # But the real token can still release it
        lock.release("u-xyz", token)
        assert lock.acquire("u-xyz", ttl_seconds=5) is not None

    def test_release_without_holding_is_noop(self, lock):
        # Releasing a key that was never locked is safe
        lock.release("never-locked", token="whatever")

    def test_wait_timeout_zero_fails_fast(self, lock):
        assert lock.acquire("u-fast", ttl_seconds=5) is not None
        t0 = time.monotonic()
        assert lock.acquire("u-fast", ttl_seconds=5, wait_timeout=0.0) is None
        assert time.monotonic() - t0 < 0.5  # didn't sleep

    def test_different_keys_dont_block_each_other(self, lock):
        assert lock.acquire("k-a", ttl_seconds=5) is not None
        # Acquiring a different key is independent
        assert lock.acquire("k-b", ttl_seconds=5) is not None


class TestServerLockIntegration:
    """Server uses LockBackend to serialize PATCH/DELETE on the same upload."""

    def _make_server(self, tmp_path, lock_backend):
        import os

        from resumable_upload.server import TusServer
        from resumable_upload.storage import SQLiteStorage

        storage = SQLiteStorage(
            db_path=os.path.join(str(tmp_path), "u.db"),
            upload_dir=os.path.join(str(tmp_path), "files"),
        )
        return TusServer(
            storage=storage,
            base_path="/files",
            lock_backend=lock_backend,
            lock_wait_seconds=0.05,
        )

    def test_patch_blocked_when_lock_held_returns_423(self, tmp_path):
        server = self._make_server(tmp_path, InMemoryLockBackend())

        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"Tus-Resumable": "1.0.0", "Upload-Length": "10"},
            b"",
        )
        location = headers["Location"]
        upload_id = location.rsplit("/", 1)[1]

        # Pre-acquire the lock outside the server so any PATCH from within
        # hits the 423 path.
        token = server._locks.acquire(upload_id, ttl_seconds=5.0)
        assert token is not None
        try:
            status, _, _ = server.handle_request(
                "PATCH",
                location,
                {
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": "0",
                    "Content-Type": "application/offset+octet-stream",
                },
                b"hi",
            )
            assert status == 423
        finally:
            server._locks.release(upload_id, token)

    def test_patch_succeeds_after_lock_released(self, tmp_path):
        server = self._make_server(tmp_path, InMemoryLockBackend())

        _, headers, _ = server.handle_request(
            "POST",
            "/files",
            {"Tus-Resumable": "1.0.0", "Upload-Length": "2"},
            b"",
        )
        location = headers["Location"]
        status, _, _ = server.handle_request(
            "PATCH",
            location,
            {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            b"hi",
        )
        assert status == 204


class TestInMemoryLockConcurrency:
    """Concurrency tests scoped to the in-memory implementation."""

    def test_only_one_holder_under_contention(self):
        lock = InMemoryLockBackend()
        winners: list[str | None] = []
        lock.acquire("shared", ttl_seconds=5)

        def try_acquire():
            winners.append(lock.acquire("shared", ttl_seconds=5, wait_timeout=0.1))

        threads = [threading.Thread(target=try_acquire) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The original acquirer holds it; all 5 racing threads time out
        assert winners.count(None) == 5
