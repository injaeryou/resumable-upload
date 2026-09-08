"""Contract tests for lock backends.

Applied to every LockBackend implementation so they agree on the
acquire/release/TTL/token-mismatch contract.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from resumable_upload.locks import InMemoryLockBackend, LockBackend

_IMPLS: list[str] = ["memory"]

try:
    import fakeredis  # noqa: F401

    from resumable_upload.locks.redis_lock import RedisLockBackend  # noqa: F401

    _IMPLS.append("redis")
except ImportError:
    pass


def _make(impl: str):
    if impl == "redis":
        import fakeredis

        from resumable_upload.locks.redis_lock import RedisLockBackend

        return RedisLockBackend(client=fakeredis.FakeRedis())
    return InMemoryLockBackend()


@pytest.fixture(params=_IMPLS)
def lock(request):
    return _make(request.param)


class PlainLockBackend(LockBackend):
    """A backend that inherits the BASE ``acquire_async`` — like ``RedisLockBackend``.

    ``InMemoryLockBackend`` overrides the async path to skip the executor
    entirely, so it can never exercise the generic bridge. Tests that care
    about the bridge use this instead.
    """

    def __init__(self) -> None:
        self._inner = InMemoryLockBackend()

    def acquire(self, key, ttl_seconds, wait_timeout=0.0):
        return self._inner.acquire(key, ttl_seconds, wait_timeout)

    def release(self, key, token):
        self._inner.release(key, token)


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


class TestDefaultLockBackend:
    """S2: TusServer defaults to an in-process lock; TusServerCore does not."""

    def _storage(self, tmp_path):
        import os

        from resumable_upload.storage import SQLiteStorage

        return SQLiteStorage(
            db_path=os.path.join(str(tmp_path), "u.db"),
            upload_dir=os.path.join(str(tmp_path), "files"),
        )

    def test_tusserver_defaults_to_in_memory_lock(self, tmp_path):
        from resumable_upload.server import TusServer

        server = TusServer(storage=self._storage(tmp_path), base_path="/files")
        assert isinstance(server._locks, InMemoryLockBackend)

    def test_tusserver_explicit_none_opts_out(self, tmp_path):
        # CLI `--lock-backend none` passes None explicitly; must stay unlocked.
        from resumable_upload.server import TusServer

        server = TusServer(storage=self._storage(tmp_path), base_path="/files", lock_backend=None)
        assert server._locks is None

    def test_core_has_no_default_lock(self, tmp_path):
        from resumable_upload.server import TusServerCore

        server = TusServerCore(storage=self._storage(tmp_path), base_path="/files")
        assert server._locks is None


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


class TestAsyncLockContract:
    """``acquire_async`` / ``release_async`` must match the sync contract."""

    def test_acquire_async_then_release_async(self, lock):
        async def scenario():
            token = await lock.acquire_async("a-1", ttl_seconds=5)
            assert token is not None
            await lock.release_async("a-1", token)
            return await lock.acquire_async("a-1", ttl_seconds=5)

        assert asyncio.run(scenario()) is not None

    def test_acquire_async_times_out_when_held(self, lock):
        async def scenario():
            assert lock.acquire("a-2", ttl_seconds=5) is not None
            return await lock.acquire_async("a-2", ttl_seconds=5, wait_timeout=0.1)

        assert asyncio.run(scenario()) is None

    def test_acquire_async_gets_lock_once_released(self, lock):
        async def scenario():
            token = lock.acquire("a-3", ttl_seconds=5)

            async def release_soon():
                await asyncio.sleep(0.05)
                lock.release("a-3", token)

            waiter = asyncio.create_task(lock.acquire_async("a-3", ttl_seconds=5, wait_timeout=2.0))
            await release_soon()
            return await waiter

        assert asyncio.run(scenario()) is not None


class TestAsyncLockDoesNotPinThreads:
    """Waiters must not hold executor threads for the whole wait_timeout.

    ``_with_lock_async`` awaits the lock on the same default executor the
    holder's storage calls (``write_chunk_async`` and friends) draw from. If a
    waiter blocks a worker for its full ``lock_wait_seconds``, enough waiters
    starve the very holder that would release them.
    """

    def test_waiters_leave_the_default_executor_free(self):
        # Deliberately NOT InMemoryLockBackend: its acquire_async override never
        # touches the executor, so it cannot fail here by construction. This has
        # to run against the generic bridge every third-party backend inherits.
        lock = PlainLockBackend()
        assert lock.acquire("busy", ttl_seconds=10) is not None

        async def scenario():
            # Fewer workers than waiters: if a waiter pins one, the probe below
            # cannot get a thread until some waiter gives up.
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2))

            waiters = [
                asyncio.create_task(lock.acquire_async("busy", ttl_seconds=10, wait_timeout=3.0))
                for _ in range(4)
            ]
            await asyncio.sleep(0.1)  # let every waiter reach its wait

            t0 = time.monotonic()
            probe = await asyncio.to_thread(lambda: "storage call")
            elapsed = time.monotonic() - t0

            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            return probe, elapsed

        probe, elapsed = asyncio.run(scenario())
        assert probe == "storage call"
        assert elapsed < 1.0, (
            f"a to_thread call queued behind lock waiters for {elapsed:.2f}s; "
            "waiters are pinning the default executor"
        )

    def test_base_acquire_async_only_polls_non_blocking(self):
        """The generic bridge must never hand a blocking wait to a thread."""
        seen: list[float] = []

        class RecordingLock(LockBackend):
            def acquire(self, key, ttl_seconds, wait_timeout=0.0):
                seen.append(wait_timeout)
                return None

            def release(self, key, token):  # pragma: no cover - never held
                pass

        backend = RecordingLock()
        assert asyncio.run(backend.acquire_async("k", ttl_seconds=5, wait_timeout=0.15)) is None
        assert seen, "acquire was never attempted"
        assert set(seen) == {0.0}, f"blocking waits leaked to the executor: {sorted(set(seen))}"


class TestAsyncLockCancellation:
    """A cancelled waiter must not strand the lock for a whole TTL.

    ASGI hosts cancel the request task when a client disconnects. A thread
    cannot be interrupted, so an acquisition attempt already in flight can
    still win the lock after its waiter is gone — the token would be lost and
    the upload locked until ``lock_ttl_seconds`` expires.
    """

    class SlowBackend(LockBackend):
        """Stands in for a network-backed lock: non-blocking, but a slow hop."""

        def __init__(self) -> None:
            self.held: str | None = None
            self.wins = 0

        def acquire(self, key, ttl_seconds, wait_timeout=0.0):
            time.sleep(0.15)
            if self.held is None:
                self.held = "tok"
                self.wins += 1
                return self.held
            return None

        def release(self, key, token):
            if self.held == token:
                self.held = None

    def test_cancelled_waiter_releases_a_lock_it_won_anyway(self):
        backend = self.SlowBackend()

        async def scenario():
            waiter = asyncio.create_task(backend.acquire_async("k", ttl_seconds=60, wait_timeout=5))
            await asyncio.sleep(0.05)  # cancel while the attempt is in flight
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            # The orphaned attempt finishes on its own schedule. Wait for it to
            # win and then be released, rather than guessing how long a loaded
            # runner needs — and keep the loop alive meanwhile, since the
            # release runs as a done-callback on this loop.
            deadline = time.monotonic() + 2.0
            while not (backend.wins and backend.held is None) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

        asyncio.run(scenario())
        assert backend.wins == 1, "the in-flight attempt never won; nothing was exercised"
        assert backend.held is None, (
            "a cancelled waiter stranded the lock; it stays held until the TTL expires"
        )


class TestAsyncServerPathNotStarvedByWaiters:
    """The regression test for the site the bug lived in: ``_with_lock_async``.

    Testing ``acquire_async`` alone is not enough — the defect was that
    ``TusServerCore._with_lock_async`` handed the *blocking* ``acquire`` to
    ``asyncio.to_thread``. The lock holder's storage ``*_async`` calls draw
    from that same default executor, so waiters pinning workers starve the
    holder that would release them.
    """

    def _server(self, tmp_path):
        import os

        from resumable_upload.server import TusServer
        from resumable_upload.storage import SQLiteStorage

        class SlowStorage(SQLiteStorage):
            """Holds the lock long enough that the holder needs a worker thread."""

            def write_chunk(self, upload_id, offset, data):
                time.sleep(0.05)
                super().write_chunk(upload_id, offset, data)

        return TusServer(
            storage=SlowStorage(
                db_path=os.path.join(str(tmp_path), "u.db"),
                upload_dir=os.path.join(str(tmp_path), "files"),
            ),
            base_path="/files",
            lock_backend=PlainLockBackend(),
            lock_wait_seconds=3.0,
        )

    def test_concurrent_async_patches_do_not_starve_the_holder(self, tmp_path):
        server = self._server(tmp_path)
        _, headers, _ = server.handle_request(
            "POST", "/files", {"Tus-Resumable": "1.0.0", "Upload-Length": "2"}, b""
        )
        location = headers["Location"]

        async def scenario():
            # Fewer workers than waiters: a waiter that pins one blocks the
            # holder's write_chunk_async from ever getting a thread.
            asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
            patch_headers = {
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            }
            t0 = time.monotonic()
            results = await asyncio.gather(
                *(
                    server.handle_request_async("PATCH", location, dict(patch_headers), b"hi")
                    for _ in range(5)
                )
            )
            return [r[0] for r in results], time.monotonic() - t0

        statuses, elapsed = asyncio.run(scenario())

        assert statuses.count(204) == 1, f"expected exactly one winner, got {statuses}"
        assert 423 not in statuses, (
            f"a waiter hit the {server._lock_wait}s lock timeout: {statuses}; "
            "the holder was starved of an executor thread"
        )
        assert elapsed < 2.0, (
            f"5 concurrent PATCHes took {elapsed:.2f}s against a "
            f"{server._lock_wait}s lock wait; waiters are pinning the executor"
        )
