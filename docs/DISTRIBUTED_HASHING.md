# 三台电脑协同计算哈希

## 架构

- `BCBH` NAS 在独立硬盘对应的本地卷上运行 PostgreSQL。三台电脑通过 TCP 连接数据库，不直接打开数据库文件。
- 源文件仍在“三维扫描存储”。每台电脑把该共享目录挂载到自己的路径。
- 队列的主键是相对共享目录的路径。Windows 盘符和 macOS `/Volumes` 路径只在各自工作节点上使用。
- 领取任务时使用 PostgreSQL 行锁和 `SKIP LOCKED`；一个任务只租给一台电脑。工作节点每分钟延长正在计算的任务租约。宕机后租约到期，其他电脑可重领。文件大小和修改时间在读取前后检查。

## 独立 NAS 上的数据库

在 `192.168.1.192` 的 Container Manager 上使用 [deploy/postgres.compose.yml](../deploy/postgres.compose.yml)。用户已确认独立硬盘是“存储空间 2”，数据目录为 `/volume2/docker/nas-deduplicator-postgres`。NAS 系统服务占用了本机 `127.0.0.1:5432`，新容器对局域网使用 **15432**（容器内部仍是 5432）。设置强密码 `NAS_DEDUP_PG_PASSWORD` 后启动 Compose。不要把 PostgreSQL 的数据目录设成 SMB/NFS 挂载路径。只向三台工作电脑开放 TCP 15432。

容器已在 `BCBH` 的存储空间 2 启动，项目名为 `nasdeduplicator`。部署配置和管理员密码位于 NAS 的 `/var/services/homes/adminbh/.nasdedup-deploy/`；Mac mini 本地另有一份私有配置。不要将密码复制到仓库或对话中。当前数据库与源文件在同一台 NAS，但 PostgreSQL 数据目录位于独立硬盘卷。

## Mac mini：迁移现有清单和哈希

迁移已在 2026-11-13 完成：3,593,815 个文件、39,765,054,657,465 字节与原 SQLite 清单一致。原 SQLite 的 130,012 条哈希中，99 条路径不在现有清单，因此迁入 129,913 条有效哈希；随后进行了少量工作节点测试。原 SQLite 与本机备份保留不变。需要重新迁移时，在项目目录安装分布式依赖，并使用管理员连接：

```sh
python3 -m pip install -r requirements-distributed.txt
export NAS_DEDUP_PG_DSN='host=192.168.1.192 port=15432 dbname=nas_deduplicator user=nas_deduplicator password=密码'
python3 distributed_hash.py --source-root '/Volumes/三维扫描存储' import
python3 distributed_hash.py status
```

迁移读取本地 `.nas-deduplicator.sqlite3` 的 `inventory` 和与清单元数据一致的 `files` 哈希。迁移可重试，已有 PostgreSQL 哈希不会被旧记录覆盖。

## 三台工作电脑

三台电脑使用权限受限的 `nas_dedup_worker` 账户。连接文件 `worker.dsn` 已保存在 NAS 的 `/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn`，仅 `adminbh` 可读。用 SSH 安全复制到各电脑的 `~/.config/nasdedup/worker.dsn`；Mac mini 已配置好。不要把管理员连接信息分发给工作节点。各电脑使用自己的源共享盘挂载路径：

```sh
# Mac mini 或 MacBook Pro；路径按实际挂载情况修改
python3 -m pip install -r requirements-distributed.txt
mkdir -p ~/.config/nasdedup
scp -O adminbh@192.168.1.192:/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn ~/.config/nasdedup/worker.dsn
chmod 600 ~/.config/nasdedup/worker.dsn
python3 distributed_hash.py status
python3 distributed_hash.py --source-root '/Volumes/三维扫描存储' --limit 10 worker
python3 distributed_hash.py --source-root '/Volumes/三维扫描存储' worker
```

```powershell
# Windows；当前已知的 SMB 地址
python -m pip install -r requirements-distributed.txt
New-Item -ItemType Directory -Force "$HOME\.config\nasdedup"
scp -O adminbh@192.168.1.192:/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn "$HOME\.config\nasdedup\worker.dsn"
python distributed_hash.py status
python distributed_hash.py --source-root '\\Bcbh\三维扫描存储' --limit 10 worker
python distributed_hash.py --source-root '\\Bcbh\三维扫描存储' worker
```

工作节点可用 Ctrl+C 停止；已提交的哈希保留。当前文件的租约到期后可以重领。用 `python distributed_hash.py status` 查看总数、完成数、正在处理数和失败数。

## 当前限制

- 新增或删除源文件后，需要重新生成清单并迁移；工作节点只处理已经入库的路径。
- `--source-root` 必须指向同一个共享目录的根。不同机器的路径可以不同。
- 容量和网络吞吐会决定实际加速比；三台电脑不保证线性三倍加速。
- Mac mini 已通过单节点与双进程并发小规模测试；Windows 和 MacBook Pro 仍需各自先用 `--limit 10 worker` 验证路径、读权限及数据库连接。详细步骤见 [工作节点操作手册](WORKER_SETUP_WINDOWS_MACBOOK.md)。
