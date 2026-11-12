"""SQLite persistence for indexed file metadata and hashes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str
    created_at: float | None
    modified_at: float


class IndexStore:
    def __init__(self, database: Path) -> None:
        self.database = database.expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at REAL, modified_at REAL
        )""")
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(files)")}
        if "created_at" not in columns:
            self.connection.execute("ALTER TABLE files ADD COLUMN created_at REAL")
        if "modified_at" not in columns:
            self.connection.execute("ALTER TABLE files ADD COLUMN modified_at REAL")
        self.connection.execute("CREATE INDEX IF NOT EXISTS files_sha256_idx ON files(sha256)")
        self.connection.commit()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def cached_hash(self, path: str, size: int, mtime_ns: int) -> str | None:
        row = self.connection.execute("SELECT sha256 FROM files WHERE path=? AND size=? AND mtime_ns=?", (path, size, mtime_ns)).fetchone()
        return row[0] if row else None

    def upsert(self, path: str, size: int, mtime_ns: int, sha256: str, created_at: float | None, modified_at: float) -> None:
        self.connection.execute("""INSERT INTO files(path,size,mtime_ns,sha256,indexed_at,created_at,modified_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP,?,?) ON CONFLICT(path) DO UPDATE SET
            size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256,
            indexed_at=CURRENT_TIMESTAMP, created_at=excluded.created_at, modified_at=excluded.modified_at""",
            (path, size, mtime_ns, sha256, created_at, modified_at))

    def update_metadata(self, path: str, created_at: float | None, modified_at: float) -> None:
        self.connection.execute("UPDATE files SET created_at=?,modified_at=? WHERE path=?", (created_at, modified_at, path))

    def all_files(self, root: Path) -> list[FileRecord]:
        rows = self.connection.execute("SELECT path,size,sha256,created_at,modified_at FROM files ORDER BY path").fetchall()
        result = []
        for row in rows:
            try:
                Path(row[0]).relative_to(root)
            except ValueError:
                continue
            result.append(FileRecord(*row))
        return result

    def remove_unseen(self, root: str, seen: set[str]) -> int:
        rows = self.connection.execute("SELECT path FROM files").fetchall()
        root_path = Path(root)
        removed = 0
        for (path,) in rows:
            try:
                Path(path).relative_to(root_path)
            except ValueError:
                continue
            if path not in seen:
                self.connection.execute("DELETE FROM files WHERE path=?", (path,))
                removed += 1
        self.connection.commit()
        return removed

    def search(self, query: str) -> list[FileRecord]:
        term = f"%{query}%"
        rows = self.connection.execute("SELECT path,size,sha256,created_at,modified_at FROM files WHERE path LIKE ? OR sha256 LIKE ? ORDER BY path", (term, term)).fetchall()
        return [FileRecord(*row) for row in rows]

    def duplicates(self, min_size: int = 1) -> list[tuple[str, list[FileRecord]]]:
        digests = self.connection.execute("SELECT sha256 FROM files WHERE size>=? GROUP BY sha256 HAVING COUNT(*)>1 ORDER BY sha256", (min_size,)).fetchall()
        groups = []
        for (digest,) in digests:
            rows = self.connection.execute("SELECT path,size,sha256,created_at,modified_at FROM files WHERE sha256=? ORDER BY path", (digest,)).fetchall()
            groups.append((digest, [FileRecord(*row) for row in rows]))
        return groups
