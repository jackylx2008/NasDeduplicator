"""Local browser controls and live scan status."""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nas_deduplicator.flows.index_nas import count_nas_files, hash_saved_inventory
from nas_deduplicator.modules.html_report import create_html_report
from nas_deduplicator.modules.index_store import IndexStore
from nas_deduplicator.modules.deployment_info import deployment_info


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NAS 文件扫描</title><style>
body{font:16px/1.6 system-ui,sans-serif;background:#f4f6fa;color:#1d2939;margin:0}main{max-width:760px;margin:8vh auto;padding:32px;background:white;border-radius:14px;box-shadow:0 8px 32px #0001}
button{background:#4355db;color:white;border:0;border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer}button:disabled{opacity:.5;cursor:default}
label{display:block;margin:12px 0}progress{width:100%;height:22px;accent-color:#4355db}.muted{color:#667085}
</style></head><body><main><h1>NAS 文件扫描</h1><p class="muted" id="root"></p>
<section id="choices"><div id="step1"><h2>第一步：选择扫描范围</h2><label><input type="radio" name="mode" value="partial" checked> 部分哈希（最多 <span id="limit"></span> 个新文件）</label>
<label><input type="radio" name="mode" value="all"> 全部文件</label><label><input type="checkbox" id="reuse"> 使用本地文件清单，跳过重新统计（已有 <span id="saved"></span> 个文件）</label>
<button id="next1">下一步</button></div><div id="step2" hidden><h2>第二步：是否计算哈希值</h2>
<label><input type="radio" name="hash" value="no" checked> 仅保存路径、修改时间和大小</label><label><input type="radio" name="hash" value="yes"> 同时计算 SHA-256 哈希</label>
<button id="back2">上一步</button> <button id="next2">下一步</button></div><div id="step3" hidden><h2>第三步：开始扫描</h2><p id="selection"></p><button id="back3">上一步</button> <button id="start">开始</button></div></section>
<section id="activity" hidden><h2 id="phase"></h2><p id="count"></p><progress id="bar"></progress><p class="muted" id="hint">扫描进行中</p><button id="stop" type="button">停止扫描并保存</button></section>
<p id="elapsed" class="muted" hidden></p>
<section id="done" hidden><h2 id="finish"></h2><p id="summary"></p><a href="/report">查看扫描结果</a></section>
<section aria-label="协作服务信息"><h2>协作服务连接</h2><p id="deployment" class="muted">正在检查…</p><small class="muted">此页面使用本地 SQLite 扫描；分布式哈希工作节点另行连接 PostgreSQL。</small></section></main>
<script>
const choices=document.querySelector('#choices'),activity=document.querySelector('#activity'),done=document.querySelector('#done'),bar=document.querySelector('#bar');
let polling=false,failures=0;
async function poll(){if(polling)return;polling=true;try{const response=await fetch('/status',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);const s=await response.json();failures=0;document.querySelector('#hint').textContent='扫描进行中';document.querySelector('#root').textContent='扫描目录：'+s.root;document.querySelector('#limit').textContent=s.limit;document.querySelector('#saved').textContent=s.saved_count;document.querySelector('#reuse').disabled=s.saved_count===0;
if(s.phase==='idle')return;choices.hidden=true;
if(s.phase==='done'){location.href='/report';return;}if(s.phase==='stopped'||s.phase==='error'){activity.hidden=true;done.hidden=false;document.querySelector('#finish').textContent=s.phase==='stopped'?'已停止并保存':'扫描失败';document.querySelector('#summary').textContent=s.message;if(s.phase==='error')done.querySelector('a').hidden=true;return;}
activity.hidden=false;document.querySelector('#phase').textContent=s.phase==='counting'?'正在统计全部文件…':'正在扫描并计算哈希…';
if(s.phase==='stopping'){document.querySelector('#phase').textContent='正在停止并保存…';document.querySelector('#stop').disabled=true;}
const elapsed=document.querySelector('#elapsed');elapsed.hidden=false;elapsed.textContent='本阶段累计耗时 '+formatTime(s.phase_elapsed)+' · 总累计耗时 '+formatTime(s.elapsed);
document.querySelector('#count').textContent=s.phase==='counting'?'已记录 '+s.count+' 个文件，容量 '+formatBytes(s.size_bytes)+'（仅读取文件信息）':'已检查 '+s.processed+' / '+s.total+' 个文件（已有索引会跳过）；清单总容量 '+formatBytes(s.size_bytes);
if(s.phase==='counting'){bar.removeAttribute('value');bar.removeAttribute('max');}else{bar.max=Math.max(s.total,1);bar.value=s.processed;}
}catch(e){failures++;if(failures>=3)document.querySelector('#hint').textContent='暂时无法获取进度，正在自动重试。';}finally{polling=false}}
const step1=document.querySelector('#step1'),step2=document.querySelector('#step2'),step3=document.querySelector('#step3');
document.querySelector('#next1').onclick=()=>{step1.hidden=true;step2.hidden=false;if(document.querySelector('#reuse').checked)document.querySelector('input[name="hash"][value="yes"]').checked=true};document.querySelector('#back2').onclick=()=>{step2.hidden=true;step1.hidden=false};
document.querySelector('#next2').onclick=()=>{const mode=document.querySelector('input[name="mode"]:checked').value,hash=document.querySelector('input[name="hash"]:checked').value,reuse=document.querySelector('#reuse').checked;if(reuse&&hash==='no'){alert('使用本地清单时请选择计算哈希');return}step2.hidden=true;step3.hidden=false;document.querySelector('#selection').textContent=(reuse?'使用本地清单':'重新统计全部文件')+'；'+(hash==='yes'?(mode==='all'?'计算全部文件哈希':'计算部分文件哈希'):'只保存文件清单')};
document.querySelector('#back3').onclick=()=>{step3.hidden=true;step2.hidden=false};
document.querySelector('#start').onclick=async()=>{const mode=document.querySelector('input[name="mode"]:checked').value,hash=document.querySelector('input[name="hash"]:checked').value,reuse=document.querySelector('#reuse').checked;document.querySelector('#start').disabled=true;const response=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,hash,reuse})});if(!response.ok){alert(await response.text());document.querySelector('#start').disabled=false;return}await poll()};
document.querySelector('#stop').onclick=async()=>{document.querySelector('#stop').disabled=true;await fetch('/stop',{method:'POST'});await poll()};
function formatTime(seconds){const n=Math.floor(seconds||0);return String(Math.floor(n/3600)).padStart(2,'0')+':'+String(Math.floor(n%3600/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0')}
function formatBytes(bytes){let n=bytes||0;for(const unit of ['B','KB','MB','GB','TB','PB']){if(n<1024||unit==='PB')return n.toFixed(unit==='B'?0:2)+' '+unit;n/=1024}}
async function pollDeployment(){try{const response=await fetch('/deployment',{cache:'no-store'});if(!response.ok)throw new Error();const d=await response.json();document.querySelector('#deployment').textContent=d.configured==='true'?'DSM '+d.dsm_url+'（'+d.dsm_tcp+'） · PostgreSQL '+d.db_endpoint+'（'+d.db_tcp+'） · 数据目录配置 '+d.data_dir+' · 检查时间 '+d.checked_at+'。'+d.scope:d.message;}catch(e){document.querySelector('#deployment').textContent='协作服务状态暂时无法取得'}}
poll();pollDeployment();setInterval(poll,1000);setInterval(pollDeployment,30000);
</script></body></html>"""


def serve_scan(root: Path, database: Path, report: Path, limit: int, force: bool, logger: logging.Logger) -> None:
    with IndexStore(database) as initial_store:
        saved_count, saved_bytes = initial_store.inventory_summary(root)
    state: dict = {"phase": "idle", "root": str(root), "limit": limit, "saved_count": saved_count, "count": 0, "size_bytes": saved_bytes, "total": 0, "processed": 0, "message": "", "started_at": None, "phase_started_at": None, "finished_at": None}
    lock = threading.Lock()
    stop_event = threading.Event()

    def update(**values: object) -> None:
        with lock:
            state.update(values)

    def run_scan(mode: str, calculate_hash: bool, reuse: bool) -> None:
        try:
            logger.info("Scan requested: mode=%s hash=%s reuse_inventory=%s root=%s limit=%d", mode, calculate_hash, reuse, root, limit)
            with IndexStore(database) as store:
                json_index = report.parent / "nas-index.json"
                count_errors = []
                if reuse:
                    total, size_bytes = store.inventory_summary(root)
                    update(count=total, size_bytes=size_bytes)
                else:
                    excluded = {report, json_index, store.database, Path(f"{store.database}-wal"), Path(f"{store.database}-shm")}
                    scan_id = store.begin_inventory()
                    counted_bytes = 0
                    counted_files = 0

                    def record_file(path, stat):
                        nonlocal counted_bytes, counted_files
                        store.record_inventory(path, stat.st_size, stat.st_mtime_ns, scan_id)
                        counted_files += 1
                        counted_bytes += stat.st_size
                        update(size_bytes=counted_bytes)
                        if counted_files % 1000 == 0:
                            store.connection.commit()

                    total, count_errors = count_nas_files(root, excluded, on_count=lambda n: update(count=n),
                                                          on_file=record_file, should_stop=stop_event.is_set)
                    store.finish_inventory(root, scan_id, complete=not stop_event.is_set() and not count_errors)
                logger.info("File count completed: total=%d errors=%d", total, len(count_errors))
                if stop_event.is_set() or not calculate_hash:
                    result = None
                else:
                    update(phase="scanning", phase_started_at=time.monotonic(), total=total)
                    result = hash_saved_inventory(root, store, force=force,
                                                  max_files=None if mode == "all" else limit,
                                                  should_stop=stop_event.is_set,
                                                  on_progress=lambda progress: update(processed=progress.scanned + progress.skipped))
                    result.errors[:0] = count_errors
                files = store.all_files(root)
                inventory_count, inventory_bytes = store.inventory_summary(root)
                create_html_report(root, files, list(set(result.directories)) if result else [], report,
                                   total_files=inventory_count, inventory_bytes=inventory_bytes, stopped=stop_event.is_set(),
                                   deployment=deployment_info())
                store.export_json(root, json_index)
                logger.info("Report and JSON index written: report=%s json=%s files=%d", report, json_index, len(files))
            detail = (f"已保存 {total} 个文件的路径和大小；本次计算哈希 {result.hashed} 个。" if result else f"已保存 {total} 个文件的路径和大小。")
            if result and result.errors:
                detail += f"有 {len(result.errors)} 个读取错误，请查看程序日志。"
            update(phase="stopped" if stop_event.is_set() else "done", saved_count=total, finished_at=time.monotonic(), message=detail)
            logger.info("Scan finished: %s", detail)
        except Exception as exc:
            logger.exception("Scan failed")
            update(phase="error", finished_at=time.monotonic(), message=str(exc))

    class Handler(BaseHTTPRequestHandler):
        def send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self.send(PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/status":
                with lock:
                    snapshot = state.copy()
                now = snapshot["finished_at"] or time.monotonic()
                snapshot["elapsed"] = now - snapshot["started_at"] if snapshot["started_at"] else 0
                snapshot["phase_elapsed"] = now - snapshot["phase_started_at"] if snapshot["phase_started_at"] else 0
                self.send(json.dumps(snapshot, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif self.path == "/deployment":
                self.send(json.dumps(deployment_info(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif self.path == "/report" and state["phase"] in ("done", "stopped"):
                self.send(report.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send(b"Not found", "text/plain; charset=utf-8", 404)

        def do_POST(self) -> None:
            if self.path == "/stop":
                with lock:
                    if state["phase"] not in ("counting", "scanning", "stopping"):
                        self.send(b"No active scan", "text/plain", 409)
                        return
                    state["phase"] = "stopping"
                stop_event.set()
                logger.info("Stop requested by user")
                self.send(b"OK", "text/plain")
                return
            if self.path != "/start":
                self.send(b"Not found", "text/plain", 404)
                return
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 1024)))
                mode = data["mode"]
                calculate_hash = data["hash"] == "yes"
                reuse = data["reuse"] is True
                if mode not in ("partial", "all") or data["hash"] not in ("yes", "no") or (reuse and not calculate_hash):
                    raise ValueError("Invalid mode")
            except (ValueError, KeyError, TypeError):
                self.send(b"Invalid request", "text/plain", 400)
                return
            with lock:
                if state["phase"] != "idle":
                    self.send(b"Scan already started", "text/plain", 409)
                    return
                state["phase"] = "counting"
                state["started_at"] = state["phase_started_at"] = time.monotonic()
            threading.Thread(target=run_scan, args=(mode, calculate_hash, reuse), daemon=True).start()
            self.send(b"OK", "text/plain")

        def log_message(self, format: str, *args: object) -> None:
            logger.debug(format, *args)

    import webbrowser
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"扫描页面：{url}（结束程序请按 Ctrl+C）", flush=True)
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
