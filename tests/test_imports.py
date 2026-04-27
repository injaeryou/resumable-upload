"""Lock down public + internal import paths after the folder refactor.

Each task of the internal refactor relocates modules but must keep every
existing import path resolving to the same class object. This test asserts
``is`` identity between old and new paths so a regression cannot silently
slip through.
"""

from __future__ import annotations

import importlib

import pytest


def _import(name: str):
    return importlib.import_module(name)


def _attr(module_name: str, attr: str):
    return getattr(_import(module_name), attr)


# ---- Storage ---------------------------------------------------------------


def test_storage_top_level_paths_unchanged():
    import resumable_upload as ru

    assert ru.Storage is _attr("resumable_upload.storage", "Storage")
    assert ru.SQLiteStorage is _attr("resumable_upload.storage", "SQLiteStorage")


def test_storage_abc_lives_in_base_submodule():
    new = _attr("resumable_upload.storage.base", "Storage")
    legacy = _attr("resumable_upload.storage", "Storage")
    assert new is legacy


def test_sqlite_storage_lives_in_sqlite_storage_submodule():
    new = _attr("resumable_upload.storage.sqlite_storage", "SQLiteStorage")
    legacy = _attr("resumable_upload.storage", "SQLiteStorage")
    assert new is legacy


@pytest.mark.parametrize(
    ("legacy_module", "new_module", "attr"),
    [
        ("resumable_upload.storage_s3", "resumable_upload.storage.s3_storage", "S3Storage"),
        ("resumable_upload.storage_gcs", "resumable_upload.storage.gcs_storage", "GCSStorage"),
        (
            "resumable_upload.storage_azure",
            "resumable_upload.storage.azure_storage",
            "AzureBlobStorage",
        ),
    ],
)
def test_cloud_storage_legacy_paths_alias_new_paths(legacy_module, new_module, attr):
    new_cls = _attr(new_module, attr)
    legacy_cls = _attr(legacy_module, attr)
    assert new_cls is legacy_cls
