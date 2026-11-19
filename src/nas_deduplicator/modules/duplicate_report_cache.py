"""Build and serve a local, read-only snapshot of shared hash jobs."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path


class DuplicateReportCache:
    """Stream PostgreSQL rows once, then answer report requests from local SQLite."""

    def __init__(self, path: Path, connect, dsn_file: Path, logger: logging.Logger,
                 refresh_hours: float = 24.0):
        self.path = path.expanduser()
        self.connect = connect
        self.dsn_file = dsn_file
        self.logger = logger
        self.refresh_seconds = max(refresh_hours, 0.25) * 3600 if math.isfinite(refresh_hours) else 24 * 3600
        self.lock = threading.Lock()
        self.state = {"running": False, "processed": 0, "message": "尚未生成报告"}

    def _db(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)

    def metadata(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with closing(self._db()) as db:
                values = dict(db.execute("SELECT key,value FROM metadata"))
            return {key: int(value) if key.endswith("_count") else value
                    for key, value in values.items()}
        except (sqlite3.Error, ValueError):
            self.logger.exception("Cannot read duplicate report cache metadata")
            return {}

    def status(self) -> dict:
        with self.lock:
            state = dict(self.state)
        return {**state, "cache": self.metadata(), "refresh_hours": self.refresh_seconds / 3600}

    def refresh(self) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update(running=True, processed=0, message="正在从 PostgreSQL 读取文件清单…")
        threading.Thread(target=self._build, name="duplicate-report-cache", daemon=True).start()
        return True

    def refresh_if_due(self) -> bool:
        built_at = self.metadata().get("built_at")
        if not built_at:
            return False  # The first snapshot starts when the report is opened.
        age = (datetime.now().astimezone() - datetime.fromisoformat(built_at)).total_seconds()
        return self.refresh() if age >= self.refresh_seconds else False

    def _build(self) -> None:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".duplicate-report-", suffix=".sqlite3", dir=self.path.parent)
            os.close(fd)
            temporary = Path(name)
            with closing(sqlite3.connect(temporary)) as local:
                local.execute("CREATE TABLE files(path TEXT PRIMARY KEY,size INTEGER NOT NULL,sha256 TEXT)")
                with self.connect(self.dsn_file) as remote:
                    with remote.cursor(name="duplicate_report_snapshot") as cursor:
                        cursor.execute("SELECT path,size,sha256 FROM hash_jobs")
                        total = hashed = 0
                        while rows := cursor.fetchmany(5000):
                            local.executemany("INSERT INTO files VALUES(?,?,?)",
                                              ((path, size, sha.strip() if sha else None) for path, size, sha in rows))
                            local.commit()
                            total += len(rows)
                            hashed += sum(sha is not None for _, _, sha in rows)
                            with self.lock:
                                self.state.update(processed=total,
                                                  message=f"已缓存 {total:,} 个文件；正在继续读取…")
                with self.lock:
                    self.state["message"] = "正在本机计算重复分组…"
                local.execute("CREATE INDEX files_size_path ON files(size,path)")
                local.execute("CREATE INDEX files_hash_path ON files(sha256,path)")
                local.execute("""CREATE TABLE hash_groups AS SELECT sha256 AS value,COUNT(*) AS file_count,
                    MIN(size) AS size, SUM(size)-MAX(size) AS possible_savings FROM files
                    WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1""")
                local.execute("""CREATE TABLE size_groups AS SELECT size AS value,COUNT(*) AS file_count,
                    size AS size, size*(COUNT(*)-1) AS possible_savings FROM files
                    GROUP BY size HAVING COUNT(*)>1""")
                local.execute("CREATE INDEX hash_groups_order ON hash_groups(possible_savings DESC,file_count DESC,value)")
                local.execute("CREATE INDEX size_groups_order ON size_groups(possible_savings DESC,file_count DESC,value)")
                local.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                group_counts = {
                    "hash_group_count": local.execute("SELECT COUNT(*) FROM hash_groups").fetchone()[0],
                    "size_group_count": local.execute("SELECT COUNT(*) FROM size_groups").fetchone()[0],
                }
                info = {"built_at": datetime.now().astimezone().isoformat(timespec="microseconds"),
                        "total_count": total, "hashed_count": hashed, **group_counts}
                local.executemany("INSERT INTO metadata VALUES(?,?)", ((key, str(value)) for key, value in info.items()))
                local.commit()
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.2)
            with self.lock:
                self.state.update(running=False, processed=total, message="报告缓存已更新")
            self.logger.info("Duplicate report cache built: total=%d hashed=%d", total, hashed)
        except Exception as exc:
            self.logger.exception("Duplicate report cache failed")
            with self.lock:
                self.state.update(running=False, message=f"报告生成失败：{exc}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def groups(self, kind: str, offset: int, limit: int = 50) -> list[dict]:
        table = self._table(kind)
        with closing(self._db()) as db:
            rows = db.execute(f"SELECT value,file_count,size,possible_savings FROM {table} "
                              "ORDER BY possible_savings DESC,file_count DESC,value LIMIT ? OFFSET ?",
                              (limit, offset)).fetchall()
        return [dict(zip(("value", "file_count", "size", "possible_savings"), row)) for row in rows]

    def members(self, kind: str, value: str, offset: int, limit: int = 100) -> list[dict]:
        self._table(kind)
        if kind == "size":
            target = int(value)
            if target < 0:
                raise ValueError("Invalid file size")
        else:
            if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
                raise ValueError("Invalid SHA-256")
            target = value.lower()
        with closing(self._db()) as db:
            rows = db.execute(f"SELECT path,size,sha256 FROM files WHERE {'size' if kind == 'size' else 'sha256'}=? "
                              "ORDER BY path LIMIT ? OFFSET ?", (target, limit, offset)).fetchall()
        return [dict(zip(("path", "size", "sha256"), row)) for row in rows]

    def confirmed_duplicates(self) -> tuple[dict, list[dict]]:
        """Return every file in a confirmed (same SHA-256) duplicate group."""
        with closing(self._db()) as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            rows = db.execute("""SELECT f.path,f.size,f.sha256 FROM files f
                JOIN hash_groups g ON g.value=f.sha256 ORDER BY f.sha256,f.path""").fetchall()
        return metadata, [dict(zip(("path", "size", "sha256"), row)) for row in rows]

    def confirmed_duplicate_pages(self, page_size: int = 50):
        """Stream confirmed hash groups in descending single-file size order."""
        with closing(self._db()) as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            cursor = db.execute("""SELECT f.path,f.size,f.sha256 FROM files f
                JOIN hash_groups g ON g.value=f.sha256
                ORDER BY g.size DESC,g.value,f.path""")
            current_hash = None
            group = []
            page = []
            for path, size, digest in cursor:
                if current_hash is not None and digest != current_hash:
                    page.append(group)
                    if len(page) >= page_size:
                        yield metadata, page
                        page = []
                    group = []
                current_hash = digest
                group.append({"path": path, "size": size, "sha256": digest})
            if group:
                page.append(group)
            if page:
                yield metadata, page

    @staticmethod
    def _table(kind: str) -> str:
        if kind not in ("hash", "size"):
            raise ValueError("Invalid report type")
        return f"{kind}_groups"
