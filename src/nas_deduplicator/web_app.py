"""Browser UI for viewing and hashing the shared PostgreSQL NAS inventory."""

from __future__ import annotations

import json
import html
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from distributed_hash import claim, connect, hash_file
from nas_deduplicator.modules.deployment_info import deployment_info
from nas_deduplicator.flows.index_nas import count_nas_files
from nas_deduplicator.modules.index_store import IndexStore
from nas_deduplicator.modules.duplicate_report_cache import DuplicateReportCache


def _clock_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("时间必须为 HH:MM")
    return hour * 60 + minute


def _inside_blackout(now: datetime, start: int, end: int) -> bool:
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def _confirmed_duplicate_page(metadata: dict, group_count: int, page_count: int) -> bytes:
    """Create the offline entry page for a folder-based duplicate report."""
    built_at = html.escape(str(metadata.get("built_at", "未知")))
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NAS Hash 确认重复报告</title><style>
body{{font:15px/1.6 system-ui,sans-serif;color:#1d2939;background:#f4f6fa;margin:0}}main{{max-width:1200px;margin:32px auto;padding:24px;background:white;border-radius:12px}}h1{{margin-top:0}}.muted{{color:#667085}}input{{box-sizing:border-box;width:100%;padding:10px;border:1px solid #ccd;border-radius:8px;font:inherit}}.wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;margin-top:12px;table-layout:fixed}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #e4e7ec;vertical-align:top}}th:first-child,td:first-child{{width:12%}}code{{overflow-wrap:anywhere}}button{{padding:7px 11px;border:0;border-radius:7px;background:#4355db;color:white;cursor:pointer}}.pager{{display:flex;gap:10px;align-items:center;margin-top:14px}}#locations{{padding:12px;background:#f9fafb;white-space:pre-wrap;overflow-wrap:anywhere}}@media(max-width:700px){{main{{margin:10px;padding:14px}}}}
</style></head><body><main><h1>NAS Hash 确认重复报告</h1>
<p class="muted">报告缓存生成时间：{built_at}。数据文件夹中包含本页和分页数据，请保持目录结构不变。</p>
<p>重复组：<strong>{group_count:,}</strong>；每页 50 组。文件大小使用 MB / GB。</p><label for="filter">筛选当前页文件名、位置或 SHA-256</label><input id="filter" type="search" placeholder="输入关键词">
<div class="wrap"><table><thead><tr><th>文件名</th><th>文件数</th><th>单个文件大小</th><th>潜在的重复容量</th><th>重复位置</th></tr></thead><tbody id="groups"></tbody></table></div>
<div class="pager"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></div><pre id="locations" hidden></pre>
<script>
const pageCount={page_count};let page=0,current=[];const fmt=n=>{{n=Number(n||0);const unit=n>=1e9?'GB':'MB';return (n/(unit==='GB'?1e9:1e6)).toLocaleString('zh-CN',{{maximumFractionDigits:2}})+' '+unit}};
function render(){{const body=document.querySelector('#groups');body.innerHTML=current.map((g,i)=>`<tr data-search="${{g.names}} ${{g.sha256}} ${{g.files.map(f=>f.path).join(' ')}}"><td>${{g.names}}</td><td>${{g.files.length.toLocaleString('zh-CN')}}</td><td>${{fmt(g.size)}}</td><td>${{fmt(g.savings)}}</td><td><button data-group="${{i}}">显示位置</button></td></tr>`).join('');document.querySelector('#page').textContent=`第 ${{page+1}} / ${{Math.max(pageCount,1)}} 页`;document.querySelector('#prev').disabled=page===0;document.querySelector('#next').disabled=page+1>=pageCount;document.querySelector('#locations').hidden=true;filterRows()}}
function loadPage(n){{page=n;const script=document.createElement('script');script.src=`data/page-${{String(page+1).padStart(5,'0')}}.js`;script.onload=()=>{{current=window.REPORT_PAGE_DATA;render();script.remove()}};script.onerror=()=>alert('无法读取数据文件，请保持 index.html 与 data 文件夹在同一目录。');document.head.appendChild(script)}}
function filterRows(){{const q=document.querySelector('#filter').value.trim().toLocaleLowerCase();document.querySelectorAll('#groups tr').forEach(row=>row.hidden=!row.dataset.search.toLocaleLowerCase().includes(q))}}
document.querySelector('#groups').addEventListener('click',event=>{{const button=event.target.closest('button[data-group]');if(!button)return;const g=current[Number(button.dataset.group)];document.querySelector('#locations').textContent='SHA-256：'+g.sha256+'\\n'+g.files.map(f=>f.path).join('\\n');document.querySelector('#locations').hidden=false}});
document.querySelector('#filter').addEventListener('input',filterRows);document.querySelector('#prev').onclick=()=>loadPage(page-1);document.querySelector('#next').onclick=()=>loadPage(page+1);loadPage(0);
</script>
</main></body></html>'''
    return page.encode("utf-8")


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NAS 协作哈希</title>
<style>body{font:16px/1.6 system-ui,sans-serif;background:#f4f6fa;color:#1d2939;margin:0}main{max-width:1000px;margin:5vh auto;padding:28px;background:white;border-radius:14px;box-shadow:0 8px 32px #0001}button,input{font:inherit;padding:9px 14px;border-radius:8px}button{background:#4355db;color:white;border:0;cursor:pointer}button:disabled{opacity:.5}input{border:1px solid #ccd}progress{width:100%;height:20px}table{width:100%;border-collapse:collapse;margin-top:12px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e7ec;overflow-wrap:anywhere}small,.muted{color:#667085}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.panel{padding:14px;border:1px solid #e4e7ec;border-radius:10px;margin:14px 0}.scroll{max-height:420px;overflow:auto}</style></head><body><main>
<h1>NAS 协作哈希</h1><p class="muted">文件清单和哈希状态来自 NAS PostgreSQL；计算哈希时仅读取数据库分配的文件；仅建立索引时会遍历 NAS 目录。</p>
<nav class="row"><button id="show-workflow" type="button">扫描任务</button><button id="show-reports" type="button">去重报告</button></nav><div id="workflow"><div id="wizard"><section class="panel" id="step1"><h2>第一步：选择路径和范围</h2><label>扫描路径：<input id="source-root" type="text" size="70" placeholder="本机挂载的 NAS 目录或其他本地目录"></label> <button id="reset-root" type="button">使用默认 NAS 路径</button><small class="muted">填写本机可访问的绝对路径；NAS 共享需先挂载。哈希任务使用 PostgreSQL 中现有的相对路径清单，所选目录须包含清单对应的文件。</small><p><label><input type="radio" name="mode" value="partial"> 部分文件（测试用）</label><label><input type="radio" name="mode" value="all" checked> 全部文件</label></p><p><label>本次处理数量：<input id="limit" type="text" inputmode="numeric" aria-label="本次处理数量" placeholder="正在读取待处理数量"></label></p><button id="next1">下一步</button></section>
<section class="panel" id="step2" hidden><h2>第二步：选择工作内容</h2><label><input type="radio" name="action" value="index"> 仅建立文件索引（扫描完整目录，保存到本机 SQLite）</label><label><input type="radio" name="action" value="hash" checked> 计算 SHA-256 哈希（根据共享数据库中的文件清单读取文件）</label><p class="muted">仅建立文件索引需要选择“全部文件”；不会读取文件内容。</p><button id="back2">上一步</button> <button id="next2">下一步</button></section>
<section class="panel" id="step3" hidden><h2>第三步：选择启动时间</h2><p id="selection"></p><label><input type="radio" name="schedule" value="now" checked> 立即开始</label><p><label><input type="radio" name="schedule" value="once"> 预约开始：<input id="start-at" type="datetime-local"></label></p><p><label><input type="radio" name="schedule" value="blackout"> 每日不允许计算时段：<input id="quiet-start" type="time" value="00:00"> 至 <input id="quiet-end" type="time" value="08:00"></label></p><small class="muted">预约需要保持程序运行；禁用时段到来时，当前文件处理完毕后暂停，时段结束自动继续。</small><p><button id="back3">上一步</button> <button id="start">确认任务</button></p></section></div>
<div id="activity" hidden><section class="panel"><h2>任务进度</h2><p id="db">连接中…</p><p id="deployment" class="muted"></p><h3>全部文件进度</h3><p id="overall-count">等待任务开始</p><progress id="overall-progress" value="0" max="1"></progress><h3>本次任务进度</h3><p id="task-count">等待任务开始</p><progress id="task-progress" value="0" max="1"></progress><p id="run" class="muted">空闲</p><small id="timing" class="muted">尚未开始任务</small><p><button id="stop" disabled>停止</button> <button id="new-task" hidden>新建任务</button></p></section>
</div></div>
<section class="panel" id="reports" hidden><h2>去重报告</h2><p class="muted">数据来自共享 PostgreSQL 的本地缓存；切换选项卡不会重新扫描数据库。</p>
<p id="report-status">正在读取缓存状态…</p><button id="report-refresh" type="button">后台刷新报告</button>
<div class="row"><button id="tab-hash" type="button">Hash 确认重复</button><button id="tab-size" type="button">大小疑似重复</button></div>
<p id="report-summary" class="muted"></p><div class="scroll"><table><thead><tr><th>Hash / 文件大小</th><th>文件数</th><th>单文件大小</th><th>可节省 / 潜在重复容量</th><th>操作</th></tr></thead><tbody id="report-groups"></tbody></table></div>
<p><button id="export-confirmed" type="button">生成 Hash 确认重复页面</button> <small class="muted">在项目 output/nas-hash-confirmed-duplicates 下生成入口和分页数据；每页 50 个重复组。</small></p><p id="export-result" class="muted"></p>
<div class="row"><button id="groups-prev">上一页</button><span id="groups-page"></span><button id="groups-next">下一页</button></div>
<div id="report-details" hidden><h3 id="details-title"></h3><div class="scroll"><table><thead><tr><th>路径</th><th>大小</th><th>SHA-256</th></tr></thead><tbody id="report-members"></tbody></table></div><div class="row"><button id="members-prev">上一页</button><span id="members-page"></span><button id="members-next">下一页</button></div></div></section></main>
<script>
const fmt=n=>Number(n||0).toLocaleString('en-US'),limitInput=document.querySelector('#limit');
const sourceInput=document.querySelector('#source-root');let defaultRoot='';fetch('/config').then(r=>r.json()).then(c=>{defaultRoot=c.default_root;sourceInput.value=defaultRoot});document.querySelector('#reset-root').onclick=()=>{sourceInput.value=defaultRoot};
const limitValue=()=>Number(limitInput.value.replaceAll(',','').trim());
let limitEdited=false;limitInput.addEventListener('input',()=>{limitEdited=true});limitInput.addEventListener('blur',()=>{const n=limitValue();if(Number.isSafeInteger(n)&&n>=0)limitInput.value=fmt(n)});
function formatSize(bytes){const n=Number(bytes||0),unit=n>=1_000_000_000?'GB':'MB',value=n/(unit==='GB'?1_000_000_000:1_000_000);return n>0&&value<0.01?'<0.01 MB':value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+' '+unit}
function duration(seconds){const n=Math.floor(seconds||0);return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(v=>String(v).padStart(2,'0')).join(':')}
let showWizardAgain=false;
function updateBar(id,done,total,unknown){const bar=document.querySelector(id);if(unknown){bar.removeAttribute('value')}else{bar.max=Math.max(total,1);bar.value=Math.min(done,total)}}
async function refresh(){
  try{
    const response=await fetch('/status',{cache:'no-store'}),s=await response.json();
    if(!response.ok||s.error)throw s.error||'无法获取任务状态';
    if(!limitEdited&&!s.running)limitInput.value=fmt(s.pending);
    const showProgress=s.has_task&&!showWizardAgain;
    document.querySelector('#wizard').hidden=showProgress;
    document.querySelector('#activity').hidden=!showProgress;
    if(!showProgress)return;
    document.querySelector('#new-task').hidden=s.running;
    document.querySelector('#stop').disabled=!s.running;
    document.querySelector('#db').textContent=s.action==='hash'?`PostgreSQL 清单：总文件 ${fmt(s.total)}；已完成哈希 ${fmt(s.done)}；待处理 ${fmt(s.pending)}`:'本机 SQLite 文件索引';
    document.querySelector('#run').textContent=(s.source_root?'路径 '+s.source_root+'；':'')+(s.message||'空闲');
    document.querySelector('#timing').textContent=s.started_at?`开始时间 ${new Date(s.started_at).toLocaleString('zh-CN')} · 任务用时 ${duration(s.elapsed)}`:'任务尚未开始';
    const counting=s.action==='index'&&s.phase==='counting',waiting=s.phase==='waiting';
    document.querySelector('#overall-count').textContent=counting?`正在统计：已发现 ${fmt(s.overall_done)} 个文件；总数统计中`:`全部文件：已处理 ${fmt(s.overall_done)} / ${fmt(s.overall_total)} 个`;
    updateBar('#overall-progress',s.overall_done,s.overall_total,counting||waiting&&s.action==='index');
    document.querySelector('#task-count').textContent=counting?'本次任务：等待总数统计完成':`本次任务：已处理 ${fmt(s.run_done)} / ${fmt(s.run_total)} 个`;
    updateBar('#task-progress',s.run_done,s.run_total,counting||waiting&&s.action==='index');
    const d=await (await fetch('/deployment',{cache:'no-store'})).json();
    document.querySelector('#deployment').textContent=d.configured==='true'?`PostgreSQL ${d.db_endpoint}（${d.db_tcp}） · 检查时间 ${d.checked_at}`:d.message;
  }catch(e){if(!document.querySelector('#activity').hidden)document.querySelector('#run').textContent='暂时无法获取进度：'+e}
}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
const mode=()=>document.querySelector('input[name="mode"]:checked').value,action=()=>document.querySelector('input[name="action"]:checked').value;
document.querySelector('#next1').onclick=()=>{if(!sourceInput.value.trim()){alert('请填写本机可访问的扫描路径');return}document.querySelector('#step1').hidden=true;document.querySelector('#step2').hidden=false};document.querySelector('#back2').onclick=()=>{document.querySelector('#step2').hidden=true;document.querySelector('#step1').hidden=false};
document.querySelector('#next2').onclick=()=>{if(action()==='index'&&mode()!=='all'){alert('仅建立文件索引需要先选择“全部文件”');return}const limit=limitValue();if(mode()==='partial'&&(!Number.isSafeInteger(limit)||limit<1)){alert('请输入大于 0 的任务数量');return}document.querySelector('#selection').textContent='路径：'+sourceInput.value.trim()+'；'+(action()==='index'?'统计全部文件的路径、大小和修改时间，保存到本机 SQLite。':(mode()==='all'?'计算 PostgreSQL 清单中全部待处理文件的哈希。':`从共享数据库获取最多 ${fmt(limit)} 个待处理文件，逐个读取内容并计算 SHA-256。`));document.querySelector('#step2').hidden=true;document.querySelector('#step3').hidden=false};document.querySelector('#back3').onclick=()=>{document.querySelector('#step3').hidden=true;document.querySelector('#step2').hidden=false};
document.querySelector('#start').onclick=async()=>{const limit=limitValue(),schedule=document.querySelector('input[name="schedule"]:checked').value,startAt=document.querySelector('#start-at').value,quietStart=document.querySelector('#quiet-start').value,quietEnd=document.querySelector('#quiet-end').value;if(schedule==='once'&&(!startAt||new Date(startAt)<=new Date())){alert('请选择未来的预约时间');return}if(schedule==='blackout'&&(!quietStart||!quietEnd||quietStart===quietEnd)){alert('请选择不同的禁用时段起止时间');return}const r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode(),action:action(),limit,source_root:sourceInput.value.trim(),schedule,start_at:schedule==='once'?new Date(startAt).toISOString():null,quiet_start:quietStart,quiet_end:quietEnd})});if(!r.ok){alert(await r.text());return}showWizardAgain=false;document.querySelector('#step3').hidden=true;limitEdited=false;refresh()};document.querySelector('#stop').onclick=async()=>{await fetch('/stop',{method:'POST'});refresh()};refresh();setInterval(refresh,2000);
document.querySelector('#new-task').onclick=()=>{showWizardAgain=true;document.querySelector('#activity').hidden=true;document.querySelector('#wizard').hidden=false;document.querySelector('#step1').hidden=false};

let reportKind='hash',groupOffset=0,memberOffset=0,groupRows=[],selectedGroup=null,reportBuiltAt=null;
const reports=document.querySelector('#reports'),workflow=document.querySelector('#workflow');
async function reportJson(url,options){const response=await fetch(url,{cache:'no-store',...options}),data=await response.json();if(!response.ok||data.error)throw new Error(data.error||'报告请求失败');return data}
async function refreshReportStatus(){
  try{
    const s=await reportJson('/report/status'),c=s.cache||{};
    document.querySelector('#report-status').textContent=(s.running?`后台生成中：已读取 ${fmt(s.processed)} 个文件；`:'')+s.message+(c.built_at?` · 上次生成 ${new Date(c.built_at).toLocaleString('zh-CN')} · 每 ${fmt(s.refresh_hours)} 小时自动刷新`:' · 尚无缓存');
    document.querySelector('#report-refresh').disabled=s.running;
    document.querySelector('#report-summary').textContent=c.built_at?`缓存文件 ${fmt(c.total_count)}；已计算 Hash ${fmt(c.hashed_count)}；Hash 重复组 ${fmt(c.hash_group_count)}；大小相同组 ${fmt(c.size_group_count)}。Hash 结果仅覆盖已计算文件；大小相同仅表示疑似重复。`:'报告首次生成后显示重复分组。';
    if(c.built_at&&c.built_at!==reportBuiltAt){reportBuiltAt=c.built_at;groupOffset=0;selectedGroup=null;document.querySelector('#report-details').hidden=true;await loadGroups()}
  }catch(e){document.querySelector('#report-status').textContent='无法读取报告状态：'+e.message}
}
async function loadGroups(){
  if(!reportBuiltAt)return;
  try{
    groupRows=await reportJson(`/report/groups?kind=${reportKind}&offset=${groupOffset}`);
    document.querySelector('#report-groups').innerHTML=groupRows.map((r,i)=>`<tr><td>${esc(reportKind==='hash'?r.value:formatSize(r.value))}</td><td>${fmt(r.file_count)}</td><td title="${fmt(r.size)} B">${formatSize(r.size)}</td><td title="${fmt(r.possible_savings)} B">${formatSize(r.possible_savings)}</td><td><button data-group="${i}">查看文件</button></td></tr>`).join('');
    document.querySelector('#groups-page').textContent=`第 ${Math.floor(groupOffset/50)+1} 页`;
    document.querySelector('#groups-prev').disabled=groupOffset===0;
    document.querySelector('#groups-next').disabled=groupRows.length<50;
  }catch(e){document.querySelector('#report-status').textContent='无法读取重复分组：'+e.message}
}
async function loadMembers(){
  if(!selectedGroup)return;
  try{
    const rows=await reportJson(`/report/members?kind=${reportKind}&value=${encodeURIComponent(selectedGroup.value)}&offset=${memberOffset}`);
    document.querySelector('#report-details').hidden=false;
    document.querySelector('#details-title').textContent=`${reportKind==='hash'?'Hash 确认重复':'大小疑似重复'}：${reportKind==='hash'?selectedGroup.value:formatSize(selectedGroup.value)}`;
    document.querySelector('#report-members').innerHTML=rows.map(r=>`<tr><td>${esc(r.path)}</td><td title="${fmt(r.size)} B">${formatSize(r.size)}</td><td>${esc(r.sha256||'未计算')}</td></tr>`).join('');
    document.querySelector('#members-page').textContent=`第 ${Math.floor(memberOffset/100)+1} 页`;
    document.querySelector('#members-prev').disabled=memberOffset===0;
    document.querySelector('#members-next').disabled=rows.length<100;
  }catch(e){document.querySelector('#report-status').textContent='无法读取组内文件：'+e.message}
}
async function requestReportRefresh(){try{await reportJson('/report/refresh',{method:'POST'});await refreshReportStatus()}catch(e){document.querySelector('#report-status').textContent='无法启动报告生成：'+e.message}}
document.querySelector('#show-workflow').onclick=()=>{reports.hidden=true;workflow.hidden=false};
document.querySelector('#show-reports').onclick=async()=>{workflow.hidden=true;reports.hidden=false;await refreshReportStatus();if(!reportBuiltAt&&!document.querySelector('#report-refresh').disabled)await requestReportRefresh()};
document.querySelector('#report-refresh').onclick=requestReportRefresh;
document.querySelector('#export-confirmed').onclick=async()=>{const button=document.querySelector('#export-confirmed'),result=document.querySelector('#export-result');button.disabled=true;button.textContent='正在生成…';result.textContent='';try{const response=await fetch('/report/export-confirmed');const data=await response.json();if(!response.ok||data.error)throw new Error(data.error||'生成失败');result.textContent=`已生成 ${data.group_count.toLocaleString('zh-CN')} 个重复组（${data.page_count} 页）：${data.output_dir}；打开 ${data.entry}` }catch(e){result.textContent='无法生成：'+e.message}finally{button.disabled=false;button.textContent='生成 Hash 确认重复页面'}};
for(const kind of ['hash','size'])document.querySelector('#tab-'+kind).onclick=()=>{reportKind=kind;groupOffset=0;selectedGroup=null;document.querySelector('#report-details').hidden=true;document.querySelector('#tab-hash').disabled=kind==='hash';document.querySelector('#tab-size').disabled=kind==='size';loadGroups()};
document.querySelector('#tab-hash').disabled=true;
document.querySelector('#report-groups').onclick=e=>{const button=e.target.closest('button[data-group]');if(!button)return;selectedGroup=groupRows[Number(button.dataset.group)];memberOffset=0;loadMembers()};
document.querySelector('#groups-prev').onclick=()=>{groupOffset=Math.max(0,groupOffset-50);loadGroups()};document.querySelector('#groups-next').onclick=()=>{groupOffset+=50;loadGroups()};
document.querySelector('#members-prev').onclick=()=>{memberOffset=Math.max(0,memberOffset-100);loadMembers()};document.querySelector('#members-next').onclick=()=>{memberOffset+=100;loadMembers()};
setInterval(()=>{if(!reports.hidden)refreshReportStatus()},10000);
</script></body></html>'''


def serve_scan(root: Path, database: Path, report: Path, limit: int, force: bool, logger: logging.Logger) -> None:
    """Serve the shared database queue and run selected jobs on this Windows worker."""
    try:
        refresh_hours = float(os.environ.get("NAS_DEDUP_REPORT_REFRESH_HOURS", "24"))
    except ValueError:
        refresh_hours = 24.0
    report_cache = DuplicateReportCache(Path.home() / ".cache/nasdedup/duplicate-report.sqlite3",
                                        connect, Path.home() / ".config/nasdedup/worker.dsn",
                                        logger, refresh_hours)
    state = {"running": False, "has_task": False, "action": None, "phase": "idle",
             "message": "空闲", "run_total": 0, "run_done": 0,
             "overall_total": 0, "overall_done": 0,
             "started_at": None, "started_monotonic": None, "finished_monotonic": None}
    lock = threading.Lock()
    summary_lock = threading.Lock()
    summary_cache: dict = {"row": None, "checked": 0.0}
    stop_event = threading.Event()
    worker_name = f"{socket.gethostname()}-html"
    blackout: tuple[int, int] | None = None

    def wait_allowed() -> bool:
        while not stop_event.is_set():
            if blackout is None or not _inside_blackout(datetime.now(), *blackout):
                return False
            with lock:
                state["message"] = "当前处于每日不允许计算时段，已暂停；时段结束后自动继续"
            stop_event.wait(15)
        return True

    def snapshot(*, fresh: bool = False) -> dict:
        with lock:
            extra = dict(state)
        if extra["action"] == "index":
            row = (0, 0, 0, 0, 0)
        else:
            try:
                with summary_lock:
                    if fresh or summary_cache["row"] is None or time.monotonic() - summary_cache["checked"] >= 15:
                        with connect(Path.home() / ".config/nasdedup/worker.dsn") as conn:
                            summary_cache["row"] = conn.execute("""SELECT count(*), count(*) FILTER (WHERE sha256 IS NOT NULL),
                                count(*) FILTER (WHERE sha256 IS NULL AND attempts < 5),
                                count(*) FILTER (WHERE sha256 IS NULL AND lease_owner IS NOT NULL AND lease_until > now()),
                                count(*) FILTER (WHERE sha256 IS NULL AND attempts >= 5)
                                FROM hash_jobs""").fetchone()
                        summary_cache["checked"] = time.monotonic()
                    row = summary_cache["row"]
            except Exception:
                if extra["action"] == "hash":
                    raise
                row = (0, 0, 0, 0, 0)
        if extra["started_monotonic"] is not None:
            extra["elapsed"] = max(0, (extra["finished_monotonic"] or time.monotonic()) - extra["started_monotonic"])
        else:
            extra["elapsed"] = 0
        extra.pop("started_monotonic")
        extra.pop("finished_monotonic")
        if extra["action"] == "hash":
            extra["overall_total"], extra["overall_done"] = row[0], row[1]
        return dict(zip(("total", "done", "pending", "leased", "failed"), row), **extra)

    def finish(**values) -> None:
        with lock:
            state.update(running=False, phase="done", finished_monotonic=time.monotonic(), **values)

    def run_index(source_root: Path) -> None:
        try:
            if not source_root.is_dir():
                raise RuntimeError(f"扫描路径不可访问：{source_root}")
            excluded = {database, report, Path(f"{database}-wal"), Path(f"{database}-shm")}
            def counted(n: int) -> None:
                if n % 1000 == 0:
                    with lock:
                        state.update(overall_done=n, message=f"正在统计文件：已发现 {n:,} 个")

            total, count_errors = count_nas_files(source_root, excluded, on_count=counted,
                                                   should_stop=wait_allowed)
            if stop_event.is_set():
                finish(overall_done=total, message=f"统计已停止；已发现 {total:,} 个文件，尚未写入新索引")
                return
            with lock:
                state.update(phase="indexing", overall_total=total, overall_done=0,
                             run_total=total, message=f"统计完成：共 {total:,} 个文件；正在建立索引")
            with IndexStore(database) as store:
                scan_id = store.begin_inventory()
                count = 0

                def record(path, stat):
                    nonlocal count
                    store.record_inventory(path, stat.st_size, stat.st_mtime_ns, scan_id)
                    count += 1
                    if count % 1000 == 0:
                        store.connection.commit()
                        with lock:
                            state.update(run_done=count, overall_done=count,
                                         message=f"正在建立索引：已记录 {count:,} 个文件")

                processed, errors = count_nas_files(source_root, excluded, on_file=record,
                                                     should_stop=wait_allowed)
                store.finish_inventory(source_root, scan_id,
                                       complete=not stop_event.is_set() and not count_errors and not errors)
            finish(run_done=processed, overall_done=processed,
                   message=f"已保存 {processed:,} 个文件到本机 SQLite" +
                   ("；已停止" if stop_event.is_set() else "") +
                   (f"；{len(count_errors) + len(errors)} 个读取错误" if count_errors or errors else ""))
        except Exception as exc:
            logger.exception("Local inventory failed")
            finish(message=f"索引失败：{exc}")

    def run_jobs(job_limit: int | None, source_root: Path) -> None:
        done = 0
        try:
            if not source_root.is_dir():
                raise RuntimeError(f"扫描路径不可访问：{source_root}")
            with connect(Path.home() / ".config/nasdedup/worker.dsn") as conn:
                sample = conn.execute("SELECT path FROM hash_jobs ORDER BY path LIMIT 20").fetchall()
                if sample and not any(source_root.joinpath(*r[0].split("/")).is_file() for r in sample):
                    raise RuntimeError("数据库中的样本文件在本机挂载路径下均不可访问，请核对 NAS_ROOT")
                while (job_limit is None or done < job_limit) and not stop_event.is_set():
                    if wait_allowed():
                        break
                    job = claim(conn, worker_name)
                    if job is None:
                        break
                    hash_file(conn, source_root, worker_name, job)
                    done += 1
                    with lock:
                        state["run_done"] = done
            message = f"已处理 {done:,} 个数据库任务" + ("；已停止" if stop_event.is_set() else "；本轮结束")
            finish(message=message)
        except Exception as exc:
            logger.exception("PostgreSQL hash worker failed")
            finish(message=f"任务失败：{exc}")

    def run_scheduled(target, args, start_at: datetime | None) -> None:
        if start_at is not None:
            while not stop_event.is_set():
                remaining = (start_at - datetime.now().astimezone()).total_seconds()
                if remaining <= 0:
                    break
                stop_event.wait(min(remaining, 30))
        if stop_event.is_set():
            finish(message="已取消预约任务")
            return
        if wait_allowed():
            finish(message="已取消等待中的任务")
            return
        if target == run_jobs and args[0] is None:
            try:
                current_pending = snapshot(fresh=True)["pending"]
            except Exception as exc:
                finish(message=f"无法读取 PostgreSQL 待处理数量：{exc}")
                return
            with lock:
                state["run_total"] = current_pending
        with lock:
            state.update(started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                         started_monotonic=time.monotonic(),
                         phase="counting" if target == run_index else "hashing",
                         message="正在统计全部文件…" if target == run_index else "正在从共享数据库获取待处理文件，并逐个读取内容计算 SHA-256…")
        target(*args)

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
            elif self.path == "/config":
                self.send_json({"default_root": str(root)})
            elif self.path == "/report/status":
                self.send_json(report_cache.status())
            elif self.path == "/report/export-confirmed":
                try:
                    pages = report_cache.confirmed_duplicate_pages(50)
                    metadata, first_page = next(pages, ({}, []))
                    if not metadata:
                        self.send_json({"error": "尚无报告缓存，请先打开去重报告并等待生成完成。"}, 409)
                        return
                    group_count = int(metadata.get("hash_group_count", 0))
                    page_count = (group_count + 49) // 50
                    project_root = Path(__file__).resolve().parents[2]
                    output_dir = project_root / "output" / "nas-hash-confirmed-duplicates"
                    data_dir = output_dir / "data"
                    data_dir.mkdir(parents=True, exist_ok=True)
                    (output_dir / "index.html").write_bytes(
                        _confirmed_duplicate_page(metadata, group_count, page_count))
                    page_groups = first_page
                    page_number = 1
                    while page_groups:
                        normalized = []
                        for files in page_groups:
                            digest = str(files[0]["sha256"])
                            names = "、".join(dict.fromkeys(Path(str(file["path"])).name for file in files))
                            size = int(files[0]["size"])
                            normalized.append({"names": html.escape(names), "sha256": digest,
                                               "size": size,
                                               "savings": sum(int(file["size"]) for file in files) - max(int(file["size"]) for file in files),
                                               "files": files})
                        (data_dir / f"page-{page_number:05d}.js").write_text(
                            "window.REPORT_PAGE_DATA=" + json.dumps(normalized, ensure_ascii=False).replace("</", "<\\/") + ";",
                            encoding="utf-8")
                        page_number += 1
                        try:
                            _, page_groups = next(pages)
                        except StopIteration:
                            page_groups = []
                    if group_count == 0:
                        (data_dir / "page-00001.js").write_text("window.REPORT_PAGE_DATA=[];", encoding="utf-8")
                    self.send_json({"output_dir": str(output_dir), "entry": str(output_dir / "index.html"),
                                    "group_count": group_count, "page_count": page_count})
                except (sqlite3.Error, OSError) as exc:
                    self.send_json({"error": str(exc)}, 500)
            elif urlparse(self.path).path in ("/report/groups", "/report/members"):
                try:
                    parsed = urlparse(self.path)
                    query = parse_qs(parsed.query)
                    kind = query.get("kind", [""])[0]
                    offset = max(0, min(int(query.get("offset", ["0"])[0]), 100_000_000))
                    if parsed.path == "/report/groups":
                        rows = report_cache.groups(kind, offset)
                    else:
                        rows = report_cache.members(kind, query.get("value", [""])[0], offset)
                    self.send_json(rows)
                except (ValueError, sqlite3.Error, OSError) as exc:
                    self.send_json({"error": str(exc)}, 400)
            elif self.path == "/status":
                try:
                    self.send_json(snapshot())
                except Exception as exc:
                    self.send_json({"error": str(exc)}, 503)
            elif self.path == "/deployment":
                self.send_json(deployment_info())
            else:
                self.send_error(404)

        def do_POST(self):
            nonlocal blackout
            if self.path == "/report/refresh":
                started = report_cache.refresh()
                self.send_json({"started": started, **report_cache.status()})
                return
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
                mode, action = data["mode"], data["action"]
                source_text = data["source_root"].strip()
                if not source_text:
                    raise ValueError
                source_root = Path(source_text).expanduser()
                if not source_root.is_absolute():
                    raise ValueError
                job_limit = int(data.get("limit", 100))
                if mode not in ("all", "partial") or action not in ("index", "hash"):
                    raise ValueError
                if action == "index" and mode != "all":
                    raise ValueError
                if mode == "partial" and job_limit < 1:
                    raise ValueError
                schedule = data["schedule"]
                if schedule not in ("now", "once", "blackout"):
                    raise ValueError
                start_at = None
                quiet = None
                if schedule == "once":
                    start_at = datetime.fromisoformat(data["start_at"].replace("Z", "+00:00"))
                    if start_at.tzinfo is None or start_at <= datetime.now().astimezone():
                        raise ValueError
                elif schedule == "blackout":
                    quiet = (_clock_minutes(data["quiet_start"]), _clock_minutes(data["quiet_end"]))
                    if quiet[0] == quiet[1]:
                        raise ValueError
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.send_json({"error": "请选择有效的范围、工作内容和启动时间"}, 400)
                return
            if action == "hash" and mode == "all":
                try:
                    job_total = snapshot(fresh=True)["pending"]
                except Exception as exc:
                    self.send_json({"error": f"无法读取 PostgreSQL 待处理数量：{exc}"}, 503)
                    return
            else:
                job_total = job_limit if action == "hash" else 0
            with lock:
                if state["running"]:
                    self.send_json({"error": "任务正在运行"}, 409)
                    return
                blackout = quiet
                state.update(running=True, has_task=True, action=action, phase="waiting",
                             overall_total=0, overall_done=0,
                             message=f"已预约 {start_at.astimezone().strftime('%Y-%m-%d %H:%M')} 启动" if start_at else
                             ("等待每日禁用时段结束…" if quiet and _inside_blackout(datetime.now(), *quiet) else "正在启动任务…"),
                             source_root=str(source_root),
                             run_total=job_total, run_done=0,
                             started_at=None, started_monotonic=None, finished_monotonic=None)
                stop_event.clear()
            target = run_index if action == "index" else run_jobs
            args = (source_root,) if action == "index" else (None if mode == "all" else job_limit, source_root)
            threading.Thread(target=run_scheduled, args=(target, args, start_at), daemon=True).start()
            self.send_json({"ok": True})

        def log_message(self, fmt, *args):
            logger.debug(fmt, *args)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        def periodic_report_refresh() -> None:
            while True:
                time.sleep(60)
                try:
                    report_cache.refresh_if_due()
                except Exception:
                    logger.exception("Periodic duplicate report refresh failed")

        threading.Thread(target=periodic_report_refresh, name="duplicate-report-scheduler", daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"NAS 协作哈希页面：{url}（结束程序请按 Ctrl+C）", flush=True)
        import webbrowser
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            stop_event.set()
