"""Create a self-contained HTML file tree report."""

from __future__ import annotations

import html
import os
import tempfile
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from nas_deduplicator.modules.index_store import FileRecord

LOGGER = logging.getLogger(__name__)


def write_progress_page(output: Path, root: Path, status: str, completed: int = 0, total: int | None = None, *, refresh: bool = True) -> Path:
    """Atomically replace the browser page while the scan is running."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    percent = min(100, completed * 100 / total) if total else 0
    progress = (f'<progress value="{completed}" max="{total}"></progress>' if total
                else '<progress></progress>')
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
{'<meta http-equiv="refresh" content="2">' if refresh else ''}<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NAS 扫描进度</title><style>body{{font:16px/1.6 system-ui,sans-serif;background:#f4f6fa;color:#1d2939;margin:0}}
main{{max-width:720px;margin:10vh auto;padding:32px;background:white;border-radius:14px;box-shadow:0 8px 32px #0001}}
progress{{width:100%;height:24px;accent-color:#4355db}}small{{color:#667085}}</style></head><body><main>
<h1>NAS 文件扫描</h1><p>扫描目录：{html.escape(str(root))}</p><h2>{html.escape(status)}</h2>
{progress}<p>{completed:,} / {total if total is not None else '统计中'} 个文件{f'（{percent:.1f}%）' if total else ''}</p>
<small>{'页面每 2 秒自动更新。扫描完成后将显示结果。' if refresh else '请检查程序输出。'}</small></main></body></html>'''
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, prefix=".nas-progress-", suffix=".html", delete=False) as stream:
        stream.write(page)
        temporary = Path(stream.name)
    os.replace(temporary, output)
    return output


def _date(timestamp: float | None) -> str:
    if timestamp is None:
        return "Unavailable"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def create_html_report(root: Path, files: list[FileRecord], directories: list[str], output: Path, *, total_files: int | None = None, stopped: bool = False, inventory_bytes: int | None = None, deployment: dict[str, str] | None = None) -> Path:
    """Write an expandable hierarchy with metadata and duplicate labels."""
    root = root.resolve()
    nodes: dict[tuple[str, ...], dict] = {(): {"dirs": set(), "files": []}}
    for directory in directories:
        try:
            relative = Path(directory).relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        for depth in range(len(parts) + 1):
            key = parts[:depth]
            nodes.setdefault(key, {"dirs": set(), "files": []})
            if depth:
                nodes.setdefault(parts[:depth - 1], {"dirs": set(), "files": []})["dirs"].add(parts[depth - 1])
    for record in files:
        try:
            relative = Path(record.path).relative_to(root)
        except ValueError:
            continue
        parent = relative.parts[:-1]
        for depth in range(len(parent) + 1):
            nodes.setdefault(parent[:depth], {"dirs": set(), "files": []})
            if depth:
                nodes.setdefault(parent[:depth - 1], {"dirs": set(), "files": []})["dirs"].add(parent[depth - 1])
        nodes[parent]["files"].append(record)
    duplicates: defaultdict[str, int] = defaultdict(int)
    for record in files:
        duplicates[record.sha256] += 1

    def render(key: tuple[str, ...], open_initially: bool = False) -> str:
        node = nodes[key]
        children = []
        for name in sorted(node["dirs"], key=str.casefold):
            child = key + (name,)
            children.append(f'<details class="folder" {"open" if open_initially else ""}><summary>📁 {html.escape(name)}</summary>{render(child)}</details>')
        if node["files"]:
            rows = []
            for record in sorted(node["files"], key=lambda item: Path(item.path).name.casefold()):
                name = Path(record.path).name
                duplicate = duplicates[record.sha256] > 1
                badge = '<span class="badge duplicate">重复</span>' if duplicate else '<span class="badge unique">唯一</span>'
                rows.append(
                    f'<tr class="file-row" data-duplicate="{str(duplicate).lower()}"><td class="filename">📄 {html.escape(name)} {badge}</td>'
                    f'<td>{_size(record.size)}</td><td>{_date(record.created_at)}</td><td>{_date(record.modified_at)}</td>'
                    f'<td class="hash" title="{html.escape(record.sha256)}">{html.escape(record.sha256)}</td></tr>'
                )
            children.append('<div class="table-wrap"><table><thead><tr><th>文件</th><th>大小</th><th>创建日期</th><th>修改日期</th><th>SHA-256</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')
        return "".join(children) or '<p class="empty">空目录</p>'

    duplicate_files = sum(1 for record in files if duplicates[record.sha256] > 1)
    deployment_html = ""
    if deployment:
        if deployment.get("configured") == "true":
            details = (
                f"DSM：{deployment['dsm_url']}（{deployment['dsm_tcp']}） · "
                f"PostgreSQL：{deployment['db_endpoint']}（{deployment['db_tcp']}） · "
                f"数据目录配置：{deployment['data_dir']} · 检查时间：{deployment['checked_at']}。"
                f"{deployment['scope']}。"
            )
        else:
            details = deployment.get("message", "未配置协作服务")
        deployment_html = f'<section class="panel"><h2>协作服务连接</h2><p>{html.escape(details)}</p><p class="muted">本报告来自本地 SQLite；分布式哈希结果保存在 PostgreSQL。</p></section>'
    report = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NAS 文件扫描报告</title><style>
:root{{--bg:#f4f6fa;--card:#fff;--ink:#1d2939;--muted:#667085;--line:#e4e7ec;--accent:#4355db;--red:#b42318;--green:#067647}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",Arial,sans-serif}}
header{{background:#182230;color:white;padding:28px max(24px,calc((100% - 1440px)/2))}}h1{{margin:0 0 6px;font-size:24px}}header p{{margin:0;color:#cbd5e1;overflow-wrap:anywhere}}
main{{max-width:1440px;margin:24px auto;padding:0 24px}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}}.stat,.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}}.stat strong{{display:block;font-size:20px}}.stat span,.muted{{color:var(--muted)}}.toolbar{{display:flex;gap:12px;align-items:center;margin-bottom:14px}}input,select{{min-width:180px;padding:10px 12px;border:1px solid #d0d5dd;border-radius:8px;font:inherit}}input{{flex:1}}button{{padding:10px 14px;border:0;border-radius:8px;background:var(--accent);color:white;cursor:pointer}}
.panel{{padding:18px}}details.folder{{margin:4px 0 4px 14px}}summary{{cursor:pointer;padding:7px 4px;font-weight:600;overflow-wrap:anywhere}}.table-wrap{{overflow:auto;margin:4px 0 12px 24px}}table{{border-collapse:collapse;width:100%;min-width:850px;font-size:12px}}th,td{{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}}th{{position:sticky;top:0;background:#f9fafb;color:#475467}}td.hash{{font-family:Consolas,monospace;max-width:320px;overflow-wrap:anywhere}}.badge{{font-size:11px;border-radius:99px;padding:2px 7px;white-space:nowrap}}.duplicate{{background:#fee4e2;color:var(--red)}}.unique{{background:#dcfae6;color:var(--green)}}.empty{{margin-left:28px;color:var(--muted)}}.hidden{{display:none!important}}@media(max-width:700px){{main{{padding:0 12px}}header{{padding:22px 16px}}.toolbar{{align-items:stretch;flex-direction:column}}}}
</style></head><body><header><h1>NAS 文件扫描报告</h1><p>扫描目录：{html.escape(str(root))}</p></header><main>
<section class="stats"><div class="stat"><strong>{total_files if total_files is not None else '未完成'}</strong><span>清单文件数</span></div><div class="stat"><strong>{_size(inventory_bytes) if inventory_bytes is not None else '未完成'}</strong><span>清单总容量</span></div><div class="stat"><strong>{len(files):,}</strong><span>已计算哈希</span></div><div class="stat"><strong>{len(directories):,}</strong><span>目录</span></div><div class="stat"><strong>{sum(duplicates[digest] > 1 for digest in duplicates):,}</strong><span>重复组</span></div><div class="stat"><strong>{duplicate_files:,}</strong><span>重复文件</span></div></section>
{deployment_html}
<p class="muted">{'扫描已停止' if stopped else '扫描完成'}：文件清单 {total_files if total_files is not None else '未完成'} 个，已计算哈希 {len(files):,} 个。NAS 回收站目录已跳过。</p>
<p class="muted">下方文件树仅显示已计算哈希的文件；全量路径与大小保存在本地 SQLite 的 inventory 表。</p>
<div class="toolbar"><select id="view-mode" aria-label="文件显示范围"><option value="all">显示全量数据</option><option value="duplicates">显示重复数据</option></select><input id="filter" type="search" placeholder="筛选文件名或哈希"><button id="expand">全部展开</button><button id="collapse">全部折叠</button></div>
<section class="panel" id="tree"><details open><summary>📂 {html.escape(root.name or str(root))}</summary>{render((), True)}</details></section></main>
<script>
const filter=document.querySelector('#filter');const viewMode=document.querySelector('#view-mode');function applyFilters(){{const q=filter.value.trim().toLocaleLowerCase();const duplicatesOnly=viewMode.value==='duplicates';document.querySelectorAll('.file-row').forEach(row=>{{const matchesText=row.textContent.toLocaleLowerCase().includes(q);const matchesMode=!duplicatesOnly||row.dataset.duplicate==='true';row.classList.toggle('hidden',!(matchesText&&matchesMode));}});document.querySelectorAll('.table-wrap').forEach(table=>table.classList.toggle('hidden',!table.querySelector('.file-row:not(.hidden)')));document.querySelectorAll('details.folder').forEach(d=>{{const hasVisible=d.querySelector('.file-row:not(.hidden)');d.classList.toggle('hidden',!hasVisible);if((q||duplicatesOnly)&&hasVisible)d.open=true;}});}}filter.addEventListener('input',applyFilters);viewMode.addEventListener('change',applyFilters);
document.querySelector('#expand').onclick=()=>document.querySelectorAll('details').forEach(d=>d.open=true);
document.querySelector('#collapse').onclick=()=>document.querySelectorAll('details.folder').forEach(d=>d.open=false);
</script></body></html>'''
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, prefix=".nas-report-", suffix=".html", delete=False) as stream:
        stream.write(report)
        temporary = Path(stream.name)
    os.replace(temporary, output)
    LOGGER.info("HTML report written: path=%s files=%d directories=%d", output, len(files), len(directories))
    return output
