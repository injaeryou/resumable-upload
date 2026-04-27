"""SQLite-backed URL storage."""

import sqlite3
from typing import Optional

from resumable_upload.url_storage.base import URLStorage


class SQLiteURLStorage(URLStorage):
    """SQLite-backed URL storage.

    Durable, concurrent-safe without application-level locking (SQLite's
    own locks serialize writes). Preferred over FileURLStorage for
    multi-process clients on the same host.
    """

    def __init__(self, db_path: str = "tus_urls.db", timeout: float = 5.0) -> None:
        self.db_path = db_path
        self.timeout = timeout
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS url_map (
                    fingerprint TEXT PRIMARY KEY,
                    url TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_url(self, fingerprint: str) -> Optional[str]:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            cur = conn.execute("SELECT url FROM url_map WHERE fingerprint = ?", (fingerprint,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_url(self, fingerprint: str, url: str) -> None:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute(
                """
                INSERT INTO url_map (fingerprint, url) VALUES (?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET url = excluded.url
                """,
                (fingerprint, url),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_url(self, fingerprint: str) -> None:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            conn.execute("DELETE FROM url_map WHERE fingerprint = ?", (fingerprint,))
            conn.commit()
        finally:
            conn.close()
