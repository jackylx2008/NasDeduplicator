"""Non-secret connection details for human review in HTML pages."""

from __future__ import annotations

import os
import socket
from datetime import datetime
from urllib.parse import urlparse


def _tcp_status(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=0.7):
            return "端口可达"
    except OSError:
        return "端口不可达"


def deployment_info() -> dict[str, str]:
    """Probe ports only; never expose or imply database authentication."""
    host = os.environ.get("NAS_DEDUP_DB_HOST", "").strip()
    port_text = os.environ.get("NAS_DEDUP_DB_PORT", "15432").strip()
    dsm_url = os.environ.get("NAS_DEDUP_DSM_URL", "").strip()
    data_dir = os.environ.get("NAS_DEDUP_PG_DATA_DIR", "").strip()
    try:
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return {"configured": "false", "message": "数据库端口配置无效"}
    if not host:
        return {"configured": "false", "message": "未配置协作数据库地址"}
    parsed = urlparse(dsm_url) if dsm_url else None
    dsm_host = parsed.hostname if parsed else None
    try:
        dsm_port = parsed.port if parsed and parsed.port else (443 if parsed and parsed.scheme == "https" else 80)
    except ValueError:
        return {"configured": "false", "message": "DSM 地址端口配置无效"}
    return {
        "configured": "true",
        "db_endpoint": f"{host}:{port}",
        "db_tcp": _tcp_status(host, port),
        "dsm_url": dsm_url or "未配置",
        "dsm_tcp": _tcp_status(dsm_host, dsm_port) if dsm_host else "未配置",
        "data_dir": data_dir or "未配置",
        "checked_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "scope": "仅检查 TCP 端口；未核验 Docker 容器健康或数据库身份认证",
    }
