"""Share one PostgreSQL hash queue across Windows and macOS workers.

The PostgreSQL server runs on the database NAS; workers mount the source NAS
locally and translate the queue's relative paths using --source-root.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path

try:
    import psycopg
except ImportError:
    raise SystemExit("请先安装依赖：python -m pip install -r requirements-distributed.txt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_jobs (
    path text PRIMARY KEY,
    size bigint NOT NULL,
    mtime_ns bigint NOT NULL,
    sha256 char(64),
    lease_owner text,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS hash_jobs_pending_idx ON hash_jobs(path)
    WHERE sha256 IS NULL AND attempts < 5;
"""
CHUNK_SIZE = 4 * 1024 * 1024
LEASE_SECONDS = 300


def connect(dsn_file: Path | None = None):
    dsn = os.environ.get("NAS_DEDUP_PG_DSN")
    if not dsn and dsn_file is not None and dsn_file.is_file():
        dsn = dsn_file.read_text(encoding="utf-8").strip()
    if not dsn:
        raise SystemExit("请设置 NAS_DEDUP_PG_DSN 或提供 --dsn-file")
    return psycopg.connect(dsn)


def initialize(conn) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def import_sqlite(conn, source: Path, root: Path, batch_size: int) -> None:
    """Resume-safe migration of inventory and completed hashes."""
    root = root.resolve()
    local = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    try:
        local.execute("SELECT 1 FROM inventory LIMIT 1")
        query = """SELECT i.path,i.size,i.mtime_ns,f.sha256
            FROM inventory i LEFT JOIN files f ON f.path=i.path AND f.size=i.size AND f.mtime_ns=i.mtime_ns
            ORDER BY i.path"""
        cursor = local.execute(query)
        total = 0
        conn.execute("CREATE TEMP TABLE incoming_jobs (path text,size bigint,mtime_ns bigint,sha256 char(64)) ON COMMIT PRESERVE ROWS")
        while batch := cursor.fetchmany(batch_size):
            rows = []
            for path, size, mtime_ns, sha in batch:
                try:
                    relative = Path(path).relative_to(root).as_posix()
                except ValueError:
                    continue
                rows.append((relative, size, mtime_ns, sha))
            with conn.cursor() as pg:
                with pg.copy("COPY incoming_jobs (path,size,mtime_ns,sha256) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
                pg.execute("""INSERT INTO hash_jobs(path,size,mtime_ns,sha256,completed_at)
                    SELECT path,size,mtime_ns,sha256,CASE WHEN sha256 IS NOT NULL THEN now() END FROM incoming_jobs
                    ON CONFLICT (path) DO NOTHING""")
                pg.execute("TRUNCATE incoming_jobs")
            conn.commit()
            total += len(rows)
            print(f"已迁移 {total:,} 条清单记录", flush=True)
        print("迁移完成；重复执行不会覆盖已完成的哈希。")
    finally:
        local.close()


def claim(conn, worker: str):
    row = conn.execute("""WITH picked AS (
        SELECT path FROM hash_jobs
        WHERE sha256 IS NULL AND attempts < 5
          AND (lease_until IS NULL OR lease_until < now())
        ORDER BY path FOR UPDATE SKIP LOCKED LIMIT 1
    )
    UPDATE hash_jobs AS job SET lease_owner=%s,
        lease_until=now()+(%s * interval '1 second'),attempts=attempts+1
    FROM picked WHERE job.path=picked.path
    RETURNING job.path,job.size,job.mtime_ns""", (worker, LEASE_SECONDS)).fetchone()
    conn.commit()
    return row


def hash_file(conn, root: Path, worker: str, job) -> None:
    relative, expected_size, expected_mtime = job
    path = root.joinpath(*relative.split("/"))
    try:
        stat = path.stat()
        if not path.is_file() or (stat.st_size, stat.st_mtime_ns) != (expected_size, expected_mtime):
            raise ValueError("文件已变化或不是普通文件；需要刷新清单")
        digest = hashlib.sha256()
        heartbeat = time.monotonic()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(chunk)
                if time.monotonic() - heartbeat >= 60:
                    conn.execute("""UPDATE hash_jobs SET lease_until=now()+(%s * interval '1 second')
                        WHERE path=%s AND lease_owner=%s AND sha256 IS NULL""", (LEASE_SECONDS, relative, worker))
                    conn.commit()
                    heartbeat = time.monotonic()
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (expected_size, expected_mtime):
            raise ValueError("读取时文件发生变化；需要刷新清单")
        conn.execute("""UPDATE hash_jobs SET sha256=%s,completed_at=now(),lease_owner=NULL,
            lease_until=NULL,last_error=NULL WHERE path=%s AND lease_owner=%s AND sha256 IS NULL""",
            (digest.hexdigest(), relative, worker))
        conn.commit()
    except (OSError, ValueError) as exc:
        conn.rollback()
        conn.execute("""UPDATE hash_jobs SET lease_owner=NULL,lease_until=now()+interval '10 minutes',
            last_error=%s WHERE path=%s AND lease_owner=%s AND sha256 IS NULL""",
            (str(exc)[:500], relative, worker))
        conn.commit()
        print(f"跳过 {relative}: {exc}", file=sys.stderr, flush=True)


def worker(conn, root: Path, name: str, limit: int | None) -> None:
    if not root.is_dir():
        raise SystemExit(f"源 NAS 共享目录不可访问：{root}")
    samples = [row[0] for row in conn.execute("SELECT path FROM hash_jobs ORDER BY path LIMIT 20")]
    conn.commit()
    if samples and not any(root.joinpath(*relative.split("/")).is_file() for relative in samples):
        raise SystemExit("数据库清单中的样本文件在本机挂载路径下均不可访问；请核对 --source-root")
    completed = 0
    while limit is None or completed < limit:
        job = claim(conn, name)
        if job is None:
            remaining = conn.execute("SELECT count(*) FROM hash_jobs WHERE sha256 IS NULL AND attempts < 5").fetchone()[0]
            conn.commit()
            if remaining == 0:
                print("可处理任务已完成。")
                return
            print(f"等待其他电脑释放任务或租约到期；剩余 {remaining:,} 个。", flush=True)
            time.sleep(10)
            continue
        hash_file(conn, root, name, job)
        completed += 1
        if completed % 100 == 0:
            print(f"{name} 已处理 {completed:,} 个任务", flush=True)


def status(conn) -> None:
    row = conn.execute("""SELECT count(*),count(*) FILTER (WHERE sha256 IS NOT NULL),
        count(*) FILTER (WHERE lease_owner IS NOT NULL AND lease_until>now()),
        count(*) FILTER (WHERE sha256 IS NULL AND attempts>=5),
        coalesce(sum(size),0) FROM hash_jobs""").fetchone()
    print(f"总文件 {row[0]:,}；已完成哈希 {row[1]:,}；正在处理 {row[2]:,}；重试耗尽 {row[3]:,}；总字节 {row[4]:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="三台电脑协同计算 NAS 哈希")
    parser.add_argument("--source-root", type=Path, help="本机挂载的源 NAS 共享目录")
    parser.add_argument("--sqlite", type=Path, default=Path(__file__).resolve().parent / ".nas-deduplicator.sqlite3")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--name", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dsn-file", type=Path, default=Path.home() / ".config/nasdedup/worker.dsn")
    parser.add_argument("action", choices=("init", "import", "worker", "status"))
    args = parser.parse_args()
    if args.action in ("import", "worker") and not args.source_root:
        parser.error("import 和 worker 需要 --source-root")
    with connect(args.dsn_file) as conn:
        if args.action in ("init", "import"):
            initialize(conn)
        if args.action == "import":
            import_sqlite(conn, args.sqlite, args.source_root, args.batch_size)
        elif args.action == "worker":
            worker(conn, args.source_root.resolve(), args.name, args.limit)
        elif args.action == "status":
            status(conn)


if __name__ == "__main__":
    main()
