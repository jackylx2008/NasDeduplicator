"""Recursive file discovery and incremental SHA-256 indexing."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nas_deduplicator.modules.index_store import IndexStore

CHUNK_SIZE = 1024 * 1024
RECYCLE_DIRECTORIES = {"#recycle", "@recycle", "$recycle.bin", ".recycle"}
MODULE_LOGGER = logging.getLogger(__name__)


@dataclass
class ScanResult:
    scanned: int = 0
    skipped: int = 0
    stopped: bool = False
    hashed: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    indexed_paths: set[str] = field(default_factory=set)


class ScanStopped(Exception):
    """The user requested a cooperative scan stop."""


def _hash_file(path: Path, should_stop: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    started = time.monotonic()
    last_log = started
    bytes_read = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            if should_stop and should_stop():
                raise ScanStopped
            digest.update(chunk)
            bytes_read += len(chunk)
            if time.monotonic() - last_log >= 10:
                MODULE_LOGGER.info("Hashing in progress: file=%s bytes=%d elapsed=%.1fs", path, bytes_read, time.monotonic() - started)
                last_log = time.monotonic()
    return digest.hexdigest()


def _walk(root: Path, onerror: Callable[[OSError], None]):
    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(name for name in dirs if name.casefold() not in RECYCLE_DIRECTORIES
                         and not (Path(current) / name).is_symlink())
        yield current, dirs, sorted(files)


def count_nas_files(root: Path, excluded_paths: set[Path] | None = None, on_count: Callable[[int], None] | None = None, should_stop: Callable[[], bool] | None = None, on_file: Callable[[Path, os.stat_result], None] | None = None) -> tuple[int, list[str]]:
    """Count eligible files before indexing, using the same traversal rules."""
    errors: list[str] = []
    excluded = {path.resolve() for path in (excluded_paths or set())}
    total = 0
    last_log = time.monotonic()
    MODULE_LOGGER.info("Counting files under %s", root)
    for current, _, files in _walk(root, lambda exc: errors.append(f"Unable to read a directory: {exc}")):
        if should_stop and should_stop():
            break
        for name in files:
            if should_stop and should_stop():
                break
            path = Path(current) / name
            try:
                if not path.is_symlink() and path.is_file() and path.resolve() not in excluded:
                    stat = path.stat()
                    if on_file:
                        on_file(path, stat)
                    total += 1
                    if on_count:
                        on_count(total)
                    if time.monotonic() - last_log >= 10:
                        MODULE_LOGGER.info("File counting in progress: found=%d", total)
                        last_log = time.monotonic()
            except OSError as exc:
                errors.append(f"Unable to inspect {path.name}: {exc}")
    MODULE_LOGGER.info("File counting finished: found=%d errors=%d", total, len(errors))
    return total, errors


def index_nas(
    root: Path,
    store: IndexStore,
    *,
    force: bool = False,
    logger: logging.Logger | None = None,
    excluded_paths: set[Path] | None = None,
    max_files: int | None = None,
    skip_cached: bool = False,
    on_progress: Callable[[ScanResult], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScanResult:
    result = ScanResult()
    MODULE_LOGGER.info("Indexing started: root=%s max_files=%s skip_cached=%s force=%s", root, max_files, skip_cached, force)
    last_log = time.monotonic()
    seen: set[str] = set()
    def record_walk_error(exc: OSError) -> None:
        result.errors.append(f"Unable to read a directory: {exc.strerror or 'access denied'}")

    ignored_db_paths = {store.database, Path(f"{store.database}-wal"), Path(f"{store.database}-shm")}
    ignored_db_paths.update(path.resolve() for path in (excluded_paths or set()))
    for current, dirs, files in _walk(root, record_walk_error):
        if should_stop and should_stop():
            result.stopped = True
            break
        result.directories.append(str(Path(current).resolve()))
        result.directories.extend(str((Path(current) / name).resolve()) for name in dirs)
        for name in files:
            if should_stop and should_stop():
                result.stopped = True
                break
            if max_files is not None and result.scanned >= max_files:
                break
            path = Path(current) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.resolve() in ignored_db_paths:
                    continue
                stat = path.stat()
                key = str(path.resolve())
                seen.add(key)
                cached = None if force else store.cached_hash(key, stat.st_size, stat.st_mtime_ns)
                if cached is not None and skip_cached:
                    result.skipped += 1
                    continue
                result.indexed_paths.add(key)
                result.scanned += 1
                if cached is not None:
                    result.unchanged += 1
                    store.update_metadata(key, _created_timestamp(stat), stat.st_mtime)
                else:
                    MODULE_LOGGER.debug("Hashing file: %s size=%d", key, stat.st_size)
                    digest = _hash_file(path, should_stop)
                    after = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        result.errors.append(f"File changed while hashing: {path.name}")
                    else:
                        store.upsert(key, stat.st_size, stat.st_mtime_ns, digest, _created_timestamp(stat), stat.st_mtime)
                        result.hashed += 1
                        MODULE_LOGGER.debug("Hash completed: %s", key)
            except ScanStopped:
                result.stopped = True
                result.scanned -= 1
                result.indexed_paths.discard(key)
                break
            except OSError as exc:
                result.errors.append(f"Unable to read {path.name}: {exc.strerror or 'I/O error'}")
                MODULE_LOGGER.warning("Unable to read file %s: %s", path, exc)
            finally:
                if on_progress:
                    on_progress(result)
                if (result.scanned + result.skipped) % 100 == 0:
                    store.connection.commit()
                if time.monotonic() - last_log >= 10:
                    MODULE_LOGGER.info("Indexing in progress: checked=%d hashed=%d skipped=%d errors=%d", result.scanned + result.skipped, result.hashed, result.skipped, len(result.errors))
                    last_log = time.monotonic()
        if result.stopped or (max_files is not None and result.scanned >= max_files):
            break
    if max_files is None and not result.errors and not result.stopped:
        store.remove_unseen(str(root.resolve()), seen)
    store.connection.commit()
    MODULE_LOGGER.info("Indexing finished: checked=%d hashed=%d skipped=%d errors=%d", result.scanned + result.skipped, result.hashed, result.skipped, len(result.errors))
    if logger:
        logger.info("Index completed: scanned=%d skipped=%d hashed=%d unchanged=%d errors=%d", result.scanned, result.skipped, result.hashed, result.unchanged, len(result.errors))
    return result


def _created_timestamp(stat: os.stat_result) -> float | None:
    """Return filesystem creation time where the platform exposes it."""
    return getattr(stat, "st_birthtime", None) or (stat.st_ctime if os.name == "nt" else None)


def hash_saved_inventory(
    root: Path, store: IndexStore, *, max_files: int | None = None, force: bool = False,
    on_progress: Callable[[ScanResult], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScanResult:
    """Hash files from saved paths; do not enumerate NAS directories again."""
    result = ScanResult()
    MODULE_LOGGER.info("Hashing saved inventory: root=%s max_files=%s", root, max_files)
    for path_text, saved_size, saved_mtime_ns in store.inventory_rows(root):
        if should_stop and should_stop():
            result.stopped = True
            break
        if max_files is not None and result.scanned >= max_files:
            break
        path = Path(path_text)
        try:
            stat = path.stat()
            if not path.is_file():
                continue
            cached = None if force else store.cached_hash(path_text, stat.st_size, stat.st_mtime_ns)
            if cached is not None:
                result.skipped += 1
                continue
            if (stat.st_size, stat.st_mtime_ns) != (saved_size, saved_mtime_ns):
                MODULE_LOGGER.info("File changed since inventory: %s", path)
                store.record_inventory(path, stat.st_size, stat.st_mtime_ns, "updated")
            digest = _hash_file(path, should_stop)
            after = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                result.errors.append(f"File changed while hashing: {path.name}")
            else:
                store.upsert(path_text, stat.st_size, stat.st_mtime_ns, digest, _created_timestamp(stat), stat.st_mtime)
                result.scanned += 1
                result.hashed += 1
                result.indexed_paths.add(path_text)
        except ScanStopped:
            result.stopped = True
            break
        except OSError as exc:
            result.errors.append(f"Unable to read {path.name}: {exc.strerror or 'I/O error'}")
            MODULE_LOGGER.warning("Unable to hash saved path %s: %s", path, exc)
        finally:
            if on_progress:
                on_progress(result)
            if (result.scanned + result.skipped) % 100 == 0:
                store.connection.commit()
    store.connection.commit()
    MODULE_LOGGER.info("Saved inventory hashing finished: hashed=%d skipped=%d errors=%d stopped=%s", result.hashed, result.skipped, len(result.errors), result.stopped)
    return result
