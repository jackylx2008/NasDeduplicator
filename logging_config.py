"""项目统一日志配置。

所有根目录入口、编排层和基础模块共享这里配置的控制台与滚动文件 handler。
默认日志位置为项目根目录下的 ``logs/<入口名>.log``。
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_MONEY_REDACTION = "*"

_MONEY_TOKEN = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY_KEY_RE = re.compile(
    rf"(?P<prefix>"
    rf"(?:['\"]?[\w\u4e00-\u9fff]*"
    rf"(?:amount|balance|price|fee|debt|due|金额|余额|欠款|应还|收入|支出|退款|手续费|价格|费用|额度)"
    rf"[\w\u4e00-\u9fff]*['\"]?\s*[:=]\s*))"
    rf"(?P<quote>['\"]?)(?P<amount>{_MONEY_TOKEN})(?!\*)(?P=quote)",
    re.IGNORECASE,
)
_MONEY_LABEL_RE = re.compile(
    rf"(?P<prefix>(?:金额|余额|欠款|应还(?:款额)?|收入|支出|退款|手续费|价格|费用|额度|总额|合计|净支出|"
    rf"最终余额|statement balance|balance due|amount due|total due)\s*(?:[:：=为]|\s)+)"
    rf"(?P<amount>{_MONEY_TOKEN})(?!\*)",
    re.IGNORECASE,
)
_CURRENCY_PREFIX_RE = re.compile(
    rf"(?P<prefix>(?:CNY|RMB|USD|GBP|JPY|SGD|HKD|TWD|人民币|人民币元|美元|英镑|日元|"
    rf"新加坡元|港币|港元|新台币|[¥￥$£])\s*[:：]?\s*)(?P<amount>{_MONEY_TOKEN})(?!\*)",
    re.IGNORECASE,
)
_CURRENCY_SUFFIX_RE = re.compile(
    rf"(?<![\w.,*])(?P<amount>{_MONEY_TOKEN})(?P<suffix>\s*/?\s*(?:CNY|RMB|USD|GBP|JPY|SGD|HKD|TWD|"
    rf"人民币|人民币元|美元|英镑|日元|新加坡元|港币|港元|新台币|元)(?![\w]))",
    re.IGNORECASE,
)
_BARE_DECIMAL_OR_GROUPED_RE = re.compile(
    r"(?<![\w.:/\\-])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+\.\d+)(?![\w.:/\\-])"
)


class MonetaryRedactingFormatter(logging.Formatter):
    """仅对持久化日志遮蔽金额，不改变控制台和 GUI 显示。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_monetary_values(super().format(record))


def setup_logger(
    log_level: int | str = logging.INFO,
    log_file: str | Path | None = None,
    *,
    entry_name: str | None = None,
    log_dir: str | Path | None = None,
) -> logging.Logger:
    """配置根 logger，同时输出到控制台和 UTF-8 滚动日志文件。"""
    level = _coerce_log_level(log_level)
    target = _resolve_log_path(log_file, entry_name=entry_name, log_dir=log_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers[:]:
        handler.close()
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    file_handler = RotatingFileHandler(
        target,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(MonetaryRedactingFormatter(LOG_FORMAT))
    root_logger.addHandler(file_handler)
    return root_logger


def _resolve_log_path(
    log_file: str | Path | None,
    *,
    entry_name: str | None,
    log_dir: str | Path | None,
) -> Path:
    if log_file is not None and log_dir is not None:
        raise ValueError("log_file 和 log_dir 不能同时指定")

    if log_file is not None:
        target = Path(log_file).expanduser()
        return target if target.is_absolute() else PROJECT_ROOT / target

    directory = Path(log_dir).expanduser() if log_dir is not None else DEFAULT_LOG_DIR
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    name = entry_name or Path(sys.argv[0]).stem or "app"
    return directory / f"{name}.log"


def get_logger(name: str | None = None) -> logging.Logger:
    """返回使用项目统一 handler 的 logger。"""
    return logging.getLogger(name)


def redact_log_monetary_values(value: object) -> str:
    """遮蔽待写入磁盘日志的金额，同时保留格式、币种和有限核对线索。"""
    text = str(value)
    text = _MONEY_KEY_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{mask_log_monetary_value(match.group('amount'))}{match.group('quote')}"
        ),
        text,
    )
    text = _MONEY_LABEL_RE.sub(
        lambda match: f"{match.group('prefix')}{mask_log_monetary_value(match.group('amount'))}",
        text,
    )
    text = _CURRENCY_PREFIX_RE.sub(
        lambda match: f"{match.group('prefix')}{mask_log_monetary_value(match.group('amount'))}",
        text,
    )
    text = _CURRENCY_SUFFIX_RE.sub(
        lambda match: f"{mask_log_monetary_value(match.group('amount'))}{match.group('suffix')}",
        text,
    )
    return _BARE_DECIMAL_OR_GROUPED_RE.sub(
        lambda match: mask_log_monetary_value(match.group(0)),
        text,
    )


def mask_log_monetary_value(value: str) -> str:
    """保留金额格式；四位及以上保留首尾数字，短金额只保留第一位。"""
    text = str(value)
    digit_positions = [index for index, character in enumerate(text) if character.isdigit()]
    if not digit_positions:
        return text
    visible = {digit_positions[0]}
    if len(digit_positions) >= 4:
        visible.add(digit_positions[-1])
    masked = "".join(
        character if not character.isdigit() or index in visible else LOG_MONEY_REDACTION
        for index, character in enumerate(text)
    )
    if len(digit_positions) == 1:
        masked += LOG_MONEY_REDACTION
    return masked


def configure_utf8_stdio() -> None:
    """在支持 ``reconfigure`` 的终端中统一使用 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _coerce_log_level(log_level: int | str) -> int:
    if isinstance(log_level, int):
        return log_level
    level = logging.getLevelName(log_level.upper())
    if not isinstance(level, int):
        raise ValueError(f"未知日志级别: {log_level}")
    return level
