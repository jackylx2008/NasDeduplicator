"""Local configuration and NAS root resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_local_environment(project_root: Path) -> None:
    """Load common.env and .env without overriding existing process variables."""
    for name in ("common.env", ".env"):
        env_file = project_root / name
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip("\"'")


def resolve_nas_root(explicit_path: Path | None = None) -> Path:
    """Resolve an explicit path or the configured local NAS root."""
    if explicit_path is not None:
        raw = str(explicit_path)
    else:
        platform_key = "LOCAL_NAS_WINDOWS" if sys.platform == "win32" else "LOCAL_NAS_MACOS" if sys.platform == "darwin" else "LOCAL_NAS_LINUX"
        raw = os.environ.get("NAS_ROOT") or os.environ.get(platform_key, "")
    if not raw.strip():
        raise ValueError("NAS path is not configured. Set NAS_ROOT or the matching LOCAL_NAS_* variable in common.env/.env.")
    root = Path(raw).expanduser().resolve()
    if not root.exists():
        raise ValueError("Configured NAS path does not exist or is unavailable.")
    if not root.is_dir():
        raise ValueError("Configured NAS path is not a directory.")
    return root
