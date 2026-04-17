"""Contract tests applied to every URLStorage backend."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from resumable_upload.url_storage import (
    FileURLStorage,
    InMemoryURLStorage,
    SQLiteURLStorage,
    URLStorage,
)


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_each(tmp_dir) -> list[URLStorage]:
    return [
        InMemoryURLStorage(),
        FileURLStorage(storage_path=os.path.join(tmp_dir, "urls.json")),
        SQLiteURLStorage(db_path=os.path.join(tmp_dir, "urls.db")),
    ]


@pytest.fixture(params=(0, 1, 2))
def storage(request, tmp_dir):
    return _make_each(tmp_dir)[request.param]


class TestURLStorageContract:
    def test_set_and_get(self, storage):
        storage.set_url("fp-1", "http://example/files/abc")
        assert storage.get_url("fp-1") == "http://example/files/abc"

    def test_missing_returns_none(self, storage):
        assert storage.get_url("never-set") is None

    def test_overwrite(self, storage):
        storage.set_url("fp-1", "http://a")
        storage.set_url("fp-1", "http://b")
        assert storage.get_url("fp-1") == "http://b"

    def test_remove(self, storage):
        storage.set_url("fp-1", "http://a")
        storage.remove_url("fp-1")
        assert storage.get_url("fp-1") is None

    def test_remove_missing_is_noop(self, storage):
        storage.remove_url("never-set")  # no raise
        assert storage.get_url("never-set") is None

    def test_multiple_keys_independent(self, storage):
        storage.set_url("a", "http://a")
        storage.set_url("b", "http://b")
        assert storage.get_url("a") == "http://a"
        assert storage.get_url("b") == "http://b"
        storage.remove_url("a")
        assert storage.get_url("a") is None
        assert storage.get_url("b") == "http://b"


class TestSQLiteURLStoragePersistence:
    def test_persists_across_reopen(self, tmp_dir):
        db = os.path.join(tmp_dir, "urls.db")
        s1 = SQLiteURLStorage(db_path=db)
        s1.set_url("fp", "http://original")
        s2 = SQLiteURLStorage(db_path=db)
        assert s2.get_url("fp") == "http://original"


class TestInMemoryURLStorageIsolation:
    def test_separate_instances_are_isolated(self):
        a = InMemoryURLStorage()
        b = InMemoryURLStorage()
        a.set_url("fp", "http://a")
        assert b.get_url("fp") is None
