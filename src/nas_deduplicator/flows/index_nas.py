"""Recursive file discovery and incremental SHA-256 indexing."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from nas_deduplicator.modules.index_store import IndexStore

CHUNK_SIZE = 1024 * 1024


@dataclass
class ScanResult:
    scanned: int = 0
    hashed: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def index_nas(
    root: Path,
    store: IndexStore,
    *,
    force: bool = False,
    logger: logging.Logger | None = None,
    excluded_paths: set[Path] | None = None,
) -> ScanResult:
    result = ScanResult()
    seen: set[str] = set()
    def record_walk_error(exc: OSError) -> None:
        result.errors.append(f"Unable to read a directory: {exc.strerror or 'access denied'}")

    ignored_db_paths = {store.database, Path(f"{store.database}-wal"), Path(f"{store.database}-shm")}
    ignored_db_paths.update(path.resolve() for path in (excluded_paths or set()))
    for current, dirs, files in os.walk(root, followlinks=False, onerror=record_walk_error):
        result.directories.append(str(Path(current).resolve()))
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        result.directories.extend(str((Path(current) / name).resolve()) for name in dirs)
        for name in files:
            path = Path(current) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.resolve() in ignored_db_paths:
                    continue
                stat = path.stat()
                key = str(path.resolve())
                seen.add(key)
                result.scanned += 1
                cached = None if force else store.cached_hash(key, stat.st_size, stat.st_mtime_ns)
                if cached is not None:
                    result.unchanged += 1
                    store.update_metadata(key, _created_timestamp(stat), stat.st_mtime)
                    continue
                digest = _hash_file(path)
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    result.errors.append(f"File changed while hashing: {path.name}")
                    continue
                store.upsert(key, stat.st_size, stat.st_mtime_ns, digest, _created_timestamp(stat), stat.st_mtime)
                result.hashed += 1
            except OSError as exc:
                result.errors.append(f"Unable to read {path.name}: {exc.strerror or 'I/O error'}")
    if not result.errors:
        store.remove_unseen(str(root.resolve()), seen)
    store.connection.commit()
    if logger:
        logger.info("Index completed: scanned=%d hashed=%d unchanged=%d errors=%d", result.scanned, result.hashed, result.unchanged, len(result.errors))
    return result


def _created_timestamp(stat: os.stat_result) -> float | None:
    """Return filesystem creation time where the platform exposes it."""
    return getattr(stat, "st_birthtime", None) or (stat.st_ctime if os.name == "nt" else None)
