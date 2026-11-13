"""Browser UI for viewing and hashing the shared PostgreSQL NAS inventory."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from distributed_hash import claim, connect, hash_file
from nas_deduplicator.modules.deployment_info import deployment_info


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NAS 协作哈希</title>
<style>body{font:16px/1.6 system-ui,sans-serif;background:#f4f6fa;color:#1d2939;margin:0}main{max-width:1000px;margin:5vh auto;padding:28px;background:white;border-radius:14px;box-shadow:0 8px 32px #0001}button,input{font:inherit;padding:9px 14px;border-radius:8px}button{background:#4355db;color:white;border:0;cursor:pointer}button:disabled{opacity:.5}input{border:1px solid #ccd}progress{width:100%;height:20px}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;overflow-wrap:anywhere}small,.muted{color:#667085}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.panel{padding:14px;border:1px solid #e4e7ec;border-radius:10px;margin:14px 0}.scroll{max-height:420px;overflow:auto}</style></head><body><main>
<h1>NAS 协作哈希</h1><p class="muted">文件清单和哈希状态来自 NAS PostgreSQL；本页面不会遍历 NAS 目录。计算哈希时仅读取数据库分配的文件。</p>
<section class="panel"><h2>远端数据库清单</h2><p id="db">连接中…</p><p id="deployment" class="muted"></p><div class="row"><label>本次处理数量：<input id="limit" type="number" min="1" value="100"></label><button id="start">开始计算哈希</button><button id="stop" disabled>停止</button></div><progress id="progress" value="0" max="1"></progress><p id="run" class="muted">空闲</p></section>
<section class="panel"><h2>远端文件列表</h2><div class="scroll"><table><thead><tr><th>路径</th><th>大小</th><th>哈希状态</th><th>SHA-256</th></tr></thead><tbody id="files"></tbody></table></div><div class="row"><button id="prev">上一页</button><span id="page" class="muted"></span><button id="next">下一页</button></div></section></main>
<script>
const fmt=n=>Number(n||0).toLocaleString();
let offset=0;async function refresh(){try{const s=await (await fetch('/status',{cache:'no-store'})).json();if(s.error)throw s.error;document.querySelector('#db').textContent=`总文件 ${fmt(s.total)}；已完成 ${fmt(s.done)}；待处理 ${fmt(s.pending)}；处理中 ${fmt(s.leased)}；重试耗尽 ${fmt(s.failed)}`;document.querySelector('#run').textContent=s.message||'空闲';document.querySelector('#start').disabled=s.running||s.pending===0;document.querySelector('#stop').disabled=!s.running;const p=document.querySelector('#progress');p.max=Math.max(s.run_total||1,1);p.value=s.run_done||0;const rows=await (await fetch('/files?offset='+offset,{cache:'no-store'})).json();if(rows.error)throw rows.error;document.querySelector('#files').innerHTML=rows.map(r=>`<tr><td>${esc(r.path)}</td><td>${fmt(r.size)}</td><td>${r.sha256?'已完成':'待处理'}</td><td>${esc(r.sha256||'')}</td></tr>`).join('');document.querySelector('#page').textContent=`第 ${Math.floor(offset/100)+1} 页（每页 100 条）`;document.querySelector('#prev').disabled=offset===0;document.querySelector('#next').disabled=rows.length<100;const d=await (await fetch('/deployment',{cache:'no-store'})).json();document.querySelector('#deployment').textContent=d.configured==='true'?`PostgreSQL ${d.db_endpoint}（${d.db_tcp}） · 检查时间 ${d.checked_at}`:d.message}catch(e){document.querySelector('#db').textContent='数据库读取失败：'+e}}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
document.querySelector('#start').onclick=async()=>{const limit=Number(document.querySelector('#limit').value);const r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit})});if(!r.ok)alert(await r.text());refresh()};document.querySelector('#stop').onclick=async()=>{await fetch('/stop',{method:'POST'});refresh()};refresh();setInterval(refresh,2000);
document.querySelector('#prev').onclick=()=>{offset=Math.max(0,offset-100);refresh()};document.querySelector('#next').onclick=()=>{offset+=100;refresh()};
</script></main></body></html>'''


def serve_scan(root: Path, database: Path, report: Path, limit: int, force: bool, logger: logging.Logger) -> None:
    """Serve the shared database queue and run selected jobs on this Windows worker."""
    state = {"running": False, "message": "空闲", "run_total": 0, "run_done": 0}
    lock = threading.Lock()
    stop_event = threading.Event()
    worker_name = f"{socket.gethostname()}-html"

    def snapshot() -> dict:
        with connect(Path.home() / ".config/nasdedup/worker.dsn") as conn:
            row = conn.execute("""SELECT count(*), count(*) FILTER (WHERE sha256 IS NOT NULL),
                count(*) FILTER (WHERE sha256 IS NULL AND attempts < 5),
                count(*) FILTER (WHERE sha256 IS NULL AND lease_owner IS NOT NULL AND lease_until > now()),
                count(*) FILTER (WHERE sha256 IS NULL AND attempts >= 5)
                FROM hash_jobs""").fetchone()
        with lock:
            extra = dict(state)
        return dict(zip(("total", "done", "pending", "leased", "failed"), row), **extra)

    def run_jobs(job_limit: int) -> None:
        done = 0
        try:
            if not root.is_dir():
                raise RuntimeError(f"源 NAS 共享目录不可访问：{root}")
            with connect(Path.home() / ".config/nasdedup/worker.dsn") as conn:
                sample = conn.execute("SELECT path FROM hash_jobs ORDER BY path LIMIT 20").fetchall()
                if sample and not any(root.joinpath(*r[0].split("/")).is_file() for r in sample):
                    raise RuntimeError("数据库中的样本文件在本机挂载路径下均不可访问，请核对 NAS_ROOT")
                while done < job_limit and not stop_event.is_set():
                    job = claim(conn, worker_name)
                    if job is None:
                        break
                    hash_file(conn, root, worker_name, job)
                    done += 1
                    with lock:
                        state["run_done"] = done
            message = f"已处理 {done:,} 个数据库任务" + ("；已停止" if stop_event.is_set() else "；本轮结束")
            with lock:
                state.update(running=False, message=message)
        except Exception as exc:
            logger.exception("PostgreSQL hash worker failed")
            with lock:
                state.update(running=False, message=f"任务失败：{exc}")

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/status":
                try:
                    self.send_json(snapshot())
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 503)
            elif urlparse(self.path).path == "/files":
                try:
                    query = parse_qs(urlparse(self.path).query)
                    offset = max(0, min(int(query.get("offset", ["0"])[0]), 100_000_000))
                    with connect(Path.home() / ".config/nasdedup/worker.dsn") as conn:
                        rows = conn.execute("SELECT path,size,sha256 FROM hash_jobs ORDER BY path LIMIT 100 OFFSET %s", (offset,)).fetchall()
                    self.send_json([{"path": r[0], "size": r[1], "sha256": r[2].strip() if r[2] else ""} for r in rows])
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 503)
            elif self.path == "/deployment":
                self.send_json(deployment_info())
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/stop":
                stop_event.set()
                self.send_json({"ok": True})
                return
            if self.path != "/start":
                self.send_error(404)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 0 < content_length <= 1024:
                    raise ValueError
                data = json.loads(self.rfile.read(content_length))
                job_limit = int(data["limit"])
                if job_limit < 1:
                    raise ValueError
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.send_json({"error": "请输入大于 0 的任务数量"}, 400)
                return
            with lock:
                if state["running"]:
                    self.send_json({"error": "任务正在运行"}, 409)
                    return
                state.update(running=True, message="正在从 PostgreSQL 领取哈希任务…", run_total=job_limit, run_done=0)
                stop_event.clear()
            threading.Thread(target=run_jobs, args=(job_limit,), daemon=True).start()
            self.send_json({"ok": True})

        def log_message(self, fmt, *args):
            logger.debug(fmt, *args)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"NAS 协作哈希页面：{url}（结束程序请按 Ctrl+C）", flush=True)
        import webbrowser
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            stop_event.set()
