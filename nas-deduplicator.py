"""Scan the configured NAS directory and open its HTML file report.

The NAS root is read from common.env/.env (NAS_ROOT or a platform-specific
LOCAL_NAS_* variable). Running this script scans files, updates their SHA-256
index, creates output/nas-report.html, and opens that report in a browser.

    python nas-deduplicator.py
    python nas-deduplicator.py --force
    python nas-deduplicator.py --root "Z:\\Shared" --report "D:\\nas-report.html"
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from logging_config import configure_utf8_stdio, get_logger, setup_logger
from nas_deduplicator.config import load_local_environment, resolve_nas_root
from nas_deduplicator.flows.index_nas import index_nas
from nas_deduplicator.modules.html_report import create_html_report
from nas_deduplicator.modules.index_store import IndexStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan the configured NAS and open a file tree report.")
    parser.add_argument("--root", type=Path, help="NAS root override; normally read from common.env/.env.")
    parser.add_argument("--database", type=Path, help="SQLite index path.")
    parser.add_argument("--report", type=Path, help="HTML report output path.")
    parser.add_argument("--force", action="store_true", help="Recalculate every file hash.")
    scan_limit = parser.add_mutually_exclusive_group()
    scan_limit.add_argument("--max-files", type=int, default=100, help="Maximum files to index (default: 100).")
    scan_limit.add_argument("--all", action="store_true", help="Index every file under the NAS root.")
    parser.add_argument("--no-open", action="store_true", help="Create the report without opening a browser.")
    parser.add_argument("--log-level", default=None, help="Log level (default: INFO).")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    load_local_environment(PROJECT_ROOT)
    args = _parser().parse_args(argv)
    if not args.all and args.max_files <= 0:
        print("--max-files must be a positive integer.", file=sys.stderr)
        return 2
    setup_logger(log_level=args.log_level or os.environ.get("LOG_LEVEL", "INFO"), entry_name="nas-deduplicator")
    logger = get_logger(__name__)

    try:
        root = resolve_nas_root(args.root)
        database = args.database or Path(os.environ.get("NAS_DEDUPLICATOR_DB", PROJECT_ROOT / ".nas-deduplicator.sqlite3"))
        report_path = args.report or Path(os.environ.get("NAS_DEDUPLICATOR_REPORT", PROJECT_ROOT / "output" / "nas-report.html"))
        with IndexStore(database) as store:
            max_files = None if args.all else args.max_files
            result = index_nas(root, store, force=args.force, logger=logger, excluded_paths={report_path}, max_files=max_files)
            files = [record for record in store.all_files(root.resolve()) if record.path in result.indexed_paths]
            report_path = create_html_report(root, files, list(set(result.directories)), report_path)

        print(f"扫描完成：发现 {result.scanned} 个文件，重新计算哈希 {result.hashed} 个，复用已有哈希 {result.unchanged} 个。")
        if not args.all and result.scanned >= args.max_files:
            print(f"本次为限量测试，仅处理按路径排序后的前 {args.max_files} 个文件；全量建档请在目标电脑运行 --all。")
        if result.errors:
            print(f"有 {len(result.errors)} 个目录或文件无法读取：")
            for error in result.errors:
                print(f"  - {error}", file=sys.stderr)
        print(f"HTML 报告：{report_path}")
        if not args.no_open:
            opened = webbrowser.open(report_path.as_uri())
            if not opened:
                print("未能自动打开浏览器，请手动打开上面的 HTML 文件路径。", file=sys.stderr)
        return 1 if result.errors else 0
    except (OSError, ValueError) as exc:
        logger.error("Operation failed: %s", exc)
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
