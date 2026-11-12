"""Measure read bandwidth from the NAS directory configured in .env.

Read-only smoke benchmark. It reads existing files and never writes to the NAS.

    python test/test_nas_bandwidth.py
    python test/test_nas_bandwidth.py --limit-mib 1024 --max-files 8
    python test/test_nas_bandwidth.py --file "Z:\\share\\large-video.mkv"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from nas_deduplicator.config import load_local_environment, resolve_nas_root

CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_LIMIT_MIB = 512
DEFAULT_TARGET_GB = 5000


def _discover_files(root: Path, *, min_size: int, max_files: int, max_directories: int) -> tuple[list[Path], int]:
    """Find a bounded sample of large files without walking the whole NAS."""
    candidates: list[tuple[int, Path]] = []
    visited_directories = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        visited_directories += 1
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in names:
            path = Path(current) / name
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
                if size >= min_size:
                    candidates.append((size, path))
            except OSError:
                continue
        if len(candidates) >= max_files * 4 or visited_directories >= max_directories:
            break
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[:max_files]], visited_directories


def _read_sample(paths: list[Path], byte_limit: int) -> tuple[int, float, list[str]]:
    total_bytes = 0
    errors: list[str] = []
    started = time.perf_counter()
    for path in paths:
        if total_bytes >= byte_limit:
            break
        try:
            with path.open("rb", buffering=0) as stream:
                while total_bytes < byte_limit:
                    block = stream.read(min(CHUNK_SIZE, byte_limit - total_bytes))
                    if not block:
                        break
                    total_bytes += len(block)
        except OSError as exc:
            errors.append(f"{path.name}: {exc.strerror or 'read error'}")
    elapsed = time.perf_counter() - started
    return total_bytes, elapsed, errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure sequential read bandwidth from the configured NAS.")
    parser.add_argument("--file", action="append", type=Path, help="Read this file (repeatable); skips automatic discovery.")
    parser.add_argument("--limit-mib", type=int, default=DEFAULT_LIMIT_MIB, help=f"Maximum bytes to read, in MiB (default: {DEFAULT_LIMIT_MIB}).")
    parser.add_argument("--min-file-mib", type=int, default=32, help="Minimum auto-selected file size in MiB (default: 32).")
    parser.add_argument("--max-files", type=int, default=8, help="Maximum files to auto-select (default: 8).")
    parser.add_argument("--max-directories", type=int, default=3000, help="Limit metadata discovery work (default: 3000).")
    parser.add_argument("--target-gb", type=float, default=DEFAULT_TARGET_GB, help=f"Total data size for time estimate in decimal GB (default: {DEFAULT_TARGET_GB}).")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.limit_mib <= 0 or args.min_file_mib < 0 or args.max_files <= 0 or args.max_directories <= 0 or args.target_gb <= 0:
        print("All size/count limits must be positive (min-file-mib may be zero).", file=sys.stderr)
        return 2

    load_local_environment(PROJECT_ROOT)
    try:
        root = resolve_nas_root()
    except (OSError, ValueError) as exc:
        print(f"NAS path configuration error: {exc}", file=sys.stderr)
        return 2

    discover_started = time.perf_counter()
    if args.file:
        files = [path.expanduser().resolve() for path in args.file]
        visited_directories = 0
    else:
        files, visited_directories = _discover_files(
            root,
            min_size=args.min_file_mib * 1024 * 1024,
            max_files=args.max_files,
            max_directories=args.max_directories,
        )
    discovery_seconds = time.perf_counter() - discover_started
    if not files:
        print("No suitable files found. Provide one or more large files with --file.", file=sys.stderr)
        return 1

    byte_limit = args.limit_mib * 1024 * 1024
    total_bytes, elapsed, errors = _read_sample(files, byte_limit)
    if total_bytes == 0 or elapsed <= 0:
        print("No file data could be read; check NAS availability and file permissions.", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    mib_per_second = total_bytes / (1024 * 1024) / elapsed
    decimal_mb_per_second = total_bytes / 1_000_000 / elapsed
    estimated_hours = args.target_gb * 1000 / decimal_mb_per_second / 3600
    print(f"NAS root: {root}")
    print(f"Sample files: {len(files)}; discovery directories visited: {visited_directories}; discovery time: {discovery_seconds:.1f} s")
    print(f"Read: {total_bytes / (1024 * 1024):.1f} MiB in {elapsed:.2f} s")
    print(f"Measured sequential read speed: {mib_per_second:.1f} MiB/s ({decimal_mb_per_second:.1f} MB/s)")
    print(f"Estimated read time for {args.target_gb:g} GB at this speed: {estimated_hours:.1f} hours")
    if errors:
        print("Some sample files could not be read:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
