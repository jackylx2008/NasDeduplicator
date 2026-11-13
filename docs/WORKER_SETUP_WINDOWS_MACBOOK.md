# Windows 与 MacBook Pro 工作节点操作手册

本项目的共同哈希数据库已部署在 `BCBH` 的 PostgreSQL 容器中，地址为 `192.168.1.192:15432`。全量文件清单已迁入。两台电脑从数据库领取**不同**的文件路径，各自从源 NAS 读取文件内容、计算 SHA-256，再把结果写回同一个数据库。工作节点不会重新列举 NAS 全部目录。

## 开始前

1. 两台电脑都能访问 `BCBH` 的“三维扫描存储”共享目录，并能连接 `192.168.1.192:15432`。
2. 使用 Python **3.10 或更新版本**。在各电脑上打开本项目目录，确认其中有 `distributed_hash.py` 和 `requirements-distributed.txt`。目前这些代码尚未提交 GitHub；请先等群晖同步完成，单独执行 `git pull` 不会得到这些未提交的文件。
3. 工作节点使用权限受限的 `worker.dsn`，不需要 PostgreSQL 管理员密码。请勿把该文件放进项目目录、群晖同步目录或 Git。
4. 先各运行 10 个任务试验，确认没有路径或权限错误，再启动长期运行。

以下命令把 Python 虚拟环境建在各电脑的用户目录，避免把 Windows/macOS 各自的依赖文件同步进项目。

## Windows 电脑（PowerShell）

在项目目录打开 PowerShell，逐行执行。`Test-Path` 应返回 `True`；若返回 `False`，先在文件资源管理器中打开 `\\Bcbh\三维扫描存储` 并完成 SMB 登录。

```powershell
Test-Path .\distributed_hash.py
Test-Path '\\Bcbh\三维扫描存储'
Test-NetConnection 192.168.1.192 -Port 15432
py -3 --version
py -3 -m venv "$HOME\nasdedup-worker-venv"
$workerPython = "$HOME\nasdedup-worker-venv\Scripts\python.exe"
& $workerPython -m pip install -r requirements-distributed.txt
```

从 NAS 管理账户的私有目录安全复制受限连接文件。此命令会要求输入 `adminbh` 的 DSM 密码；密码只在 PowerShell 中输入。`-O` 使用 DSM 当前可用的旧式 SCP 传输。

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\nasdedup" | Out-Null
scp -O adminbh@192.168.1.192:/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn "$HOME\.config\nasdedup\worker.dsn"
Test-Path "$HOME\.config\nasdedup\worker.dsn"
```

先查看共同数据库状态，再试算 10 个任务。结束后再运行一次 `status`，确认哈希完成数增长、正在处理数最终回到 0。

```powershell
& $workerPython distributed_hash.py status
& $workerPython distributed_hash.py --source-root '\\Bcbh\三维扫描存储' --limit 10 worker
& $workerPython distributed_hash.py status
```

试运行成功后，在保持开启的 PowerShell 窗口中执行长期任务：

```powershell
$workerPython = "$HOME\nasdedup-worker-venv\Scripts\python.exe"
& $workerPython distributed_hash.py --source-root '\\Bcbh\三维扫描存储' worker
```

## MacBook Pro（终端）

在 Finder 中选择“前往 → 连接服务器”，连接 `smb://Bcbh/三维扫描存储/`。然后在项目目录打开终端。Finder 挂载后检查实际路径；若显示为 `/Volumes/三维扫描存储-1` 等名称，下面命令中的路径也要相应改为该名称。

```sh
ls /Volumes
test -d '/Volumes/三维扫描存储' && echo '共享目录可访问'
nc -vz -G 3 192.168.1.192 15432
python3 --version
mkdir -p "$HOME/.local/share"
python3 -m venv "$HOME/.local/share/nasdedup-worker-venv"
worker_python="$HOME/.local/share/nasdedup-worker-venv/bin/python"
"$worker_python" -m pip install -r requirements-distributed.txt
```

复制受限连接文件并限制本机权限；SSH 会要求输入 DSM 密码。

```sh
mkdir -p ~/.config/nasdedup
scp -O adminbh@192.168.1.192:/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn ~/.config/nasdedup/worker.dsn
chmod 600 ~/.config/nasdedup/worker.dsn
```

先试算 10 个任务，再启动长期任务。根据 `ls /Volumes` 的结果调整 `--source-root`。

```sh
"$worker_python" distributed_hash.py status
"$worker_python" distributed_hash.py --source-root '/Volumes/三维扫描存储' --limit 10 worker
"$worker_python" distributed_hash.py status
worker_python="$HOME/.local/share/nasdedup-worker-venv/bin/python"
"$worker_python" distributed_hash.py --source-root '/Volumes/三维扫描存储' worker
```

## 运行与故障处理

- 工作窗口应保持打开。按 `Ctrl+C` 可停止当前工作节点；已完成的哈希不会丢失。当前文件若尚未写回，租约到期后会由其他节点重新领取。
- `status` 显示总文件数、已完成哈希数、正在处理数和重试耗尽数。三台电脑可以随时执行该只读命令。
- 若提示“源 NAS 共享目录不可访问”，先检查 SMB 挂载和 `--source-root`。若样本文件均不可访问，通常是挂载到了不同共享目录或路径写错。
- 若 `scp -O` 报“未知选项”，请运行 `ssh -V` 查看本机 OpenSSH 版本，并把错误输出发回 Mac mini；不要将 `worker.dsn` 内容贴到聊天或邮件。
- 若 `status` 连接失败，检查 `192.168.1.192:15432` 网络连通性与 `worker.dsn` 是否完整。不要改动 NAS 上的 PostgreSQL 数据目录。
- 每台电脑完成 10 个任务的测试后，再启动三台长期任务。任务由数据库动态分配，速度快的电脑会自动领取更多文件，无需人工按三等份切清单。
