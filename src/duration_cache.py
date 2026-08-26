"""SQLite-backed duration cache.

Cache key: (absolute_path, mtime_ns, filesize_bytes)
This means a re-run on an unchanged file skips mutagen entirely.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS duration_cache (
    filepath  TEXT NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    filesize  INTEGER NOT NULL,
    duration  REAL,
    PRIMARY KEY (filepath, mtime_ns, filesize)
);
"""

LOOKUP_SQL = """
SELECT duration FROM duration_cache
WHERE filepath = ? AND mtime_ns = ? AND filesize = ?;
"""

INSERT_SQL = """
INSERT OR REPLACE INTO duration_cache (filepath, mtime_ns, filesize, duration)
VALUES (?, ?, ?, ?);
"""


class DurationCache:
    """Persistent SQLite cache mapping (path, mtime, size) → duration seconds."""

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the SQLite database at *db_path*."""
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "DurationCache":
        """Open the database connection."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute(CREATE_TABLE_SQL)
        self._conn.commit()
        return self

    def __exit__(self, *_: object) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _cache_key(self, filepath: Path) -> tuple[str, int, int]:
        """Return (abs_path_str, mtime_ns, filesize) for *filepath*."""
        stat = filepath.stat()
        return (str(filepath.resolve()), stat.st_mtime_ns, stat.st_size)

    def get(self, filepath: Path) -> Optional[float]:
        """Return cached duration or None if not cached / stale."""
        if self._conn is None:
            raise RuntimeError("DurationCache must be used as a context manager.")
        key = self._cache_key(filepath)
        cursor = self._conn.execute(LOOKUP_SQL, key)
        row = cursor.fetchone()
        if row is not None:
            logger.debug("Cache hit: %s", filepath.name)
            return row[0]  # may be None if extraction previously failed
        return None

    def set(self, filepath: Path, duration: Optional[float]) -> None:
        """Store *duration* for *filepath* in the cache."""
        if self._conn is None:
            raise RuntimeError("DurationCache must be used as a context manager.")
        key = self._cache_key(filepath)
        self._conn.execute(INSERT_SQL, (*key, duration))

    def contains(self, filepath: Path) -> bool:
        """Return True if a cache entry exists for *filepath* (may hold None)."""
        if self._conn is None:
            raise RuntimeError("DurationCache must be used as a context manager.")
        key = self._cache_key(filepath)
        cursor = self._conn.execute(
            "SELECT 1 FROM duration_cache WHERE filepath=? AND mtime_ns=? AND filesize=?",
            key,
        )
        return cursor.fetchone() is not None

    def clear(self) -> None:
        """Remove all entries from the cache."""
        if self._conn is None:
            raise RuntimeError("DurationCache must be used as a context manager.")
        self._conn.execute("DELETE FROM duration_cache")
        self._conn.commit()
        logger.info("Duration cache cleared.")
