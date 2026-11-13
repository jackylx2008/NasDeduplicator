# NasDeduplicator 进度交接（2026-11-13，Asia/Shanghai）

## 当前结果

- 源文件在群晖 `BCBH` 的“三维扫描存储”共享目录。Mac mini 挂载路径 `/Volumes/三维扫描存储`；Windows SMB 路径 `\\Bcbh\三维扫描存储`；用户后续明确第三台电脑是 **MacBook Pro**，其实际 `/Volumes` 路径仍未知。
- Mac mini 原 SQLite `.nas-deduplicator.sqlite3` 保留不变。全量清单 **3,593,815** 个文件、总大小 **39,765,054,657,465** 字节；原哈希 **130,012** 条。SQLite 在线备份在 `/Users/dagentdev/.local/share/nasdeduplicator/backups/.nas-deduplicator-20261113-092029.sqlite3`，校验为 `ok`。
- 单机全量哈希任务已通过 `/stop` 正常停止。旧浏览器服务可能仍在运行，但其扫描状态是 `stopped`；重启后请重新核实进程状态。
- PostgreSQL 17 容器已在同一台 BCBH NAS 的**存储空间 2**上启动，数据目录 `/volume2/docker/nas-deduplicator-postgres`，局域网地址 `192.168.1.192:15432`。DSM 系统 PostgreSQL 占用 `127.0.0.1:5432`，因此容器主机端口改为 15432。Compose 项目名 `nasdeduplicator`，配置在 NAS 的 `/var/services/homes/adminbh/.nasdedup-deploy/`。
- 完整清单已迁移到 PostgreSQL，文件数与总容量和 SQLite **完全一致**。原 130,012 条哈希中，99 条路径已不在当前全量清单，故仅迁入 **129,913** 条有效哈希；之后做了少量工作节点测试。最近一次 `status` 显示 **129,938** 条哈希、0 活跃租约、0 重试耗尽任务。
- PostgreSQL 压缩备份已保存在 Mac mini `/Users/dagentdev/.local/share/nasdeduplicator/backups/postgres-20261113-110251.dump`（约 38 MB）；`pg_restore` 已验证归档中 `hash_jobs` 表有 **3,593,815** 行，解压命令退出码为 0。
- **三台电脑的长期哈希工作节点尚未启动。** Mac mini 仅进行了单节点、两个并发节点及受限账户的小规模验证。Windows 和 MacBook Pro 尚未接入；两台机器的具体操作见 `docs/WORKER_SETUP_WINDOWS_MACBOOK.md`。

## 凭据与访问

- SSH 管理账户 `adminbh`，端口 22；专用密钥在本机 `~/.ssh/nasdedup_dsm_ed25519`，公钥已安装在 NAS。不要将私钥加入仓库。
- PostgreSQL 管理员密码在本机 `~/.config/nasdedup/postgres.env` 和 NAS 私有部署目录。受限工作账户 `nas_dedup_worker` 的连接文件在本机 `~/.config/nasdedup/worker.dsn` 与 NAS 的 `/var/services/homes/adminbh/.nasdedup-deploy/worker.dsn`，文件权限 600。不要把密码发到对话或提交 Git。
- `adminbh` 无权直接访问 root-only Docker socket；容器启动时由用户在本机终端执行 `ssh -t ... 'sudo .../start.sh'` 并本地输入 DSM 密码。

## 代码与部署文件

- 单机扫描及浏览器 UI：`nas-deduplicator.py`、`src/nas_deduplicator/web_app.py`、`src/nas_deduplicator/flows/index_nas.py`、`src/nas_deduplicator/modules/index_store.py`、`src/nas_deduplicator/modules/html_report.py`。
- `src/nas_deduplicator/modules/deployment_info.py` 为 HTML 操作页与结果页提供非敏感的 DSM/PostgreSQL 地址、数据目录配置和 TCP 端口探测；只反映端口连通，不代表容器健康或数据库认证成功。
- 分布式程序：`distributed_hash.py`，使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 单文件动态领取、5 分钟租约、计算期间续约、文件元数据核对、断线后重领。
- 部署文件：`deploy/postgres.compose.yml`、`deploy/postgres.env.example`、`requirements-distributed.txt`；操作说明见 `docs/DISTRIBUTED_HASHING.md`。
- 本轮改动总结见 `docs/UPDATE_2026-11-13.md`。
- **以上仓库改动尚未提交 Git。** 不要重置或覆盖工作区。

## 已验证

- NAS 上的 Compose 配置有效；PostgreSQL 17.12 可通过 `192.168.1.192:15432` 登录，数据目录映射到 `/volume2`。
- 20 条真实记录迁移测试成功；完整 3,593,815 条迁移耗时约 27 分钟，按批提交，可安全重试。
- Mac mini 从源共享盘读取文件并写回 PostgreSQL：单节点 2 条、双进程并发各 10 条、受限工作账户 1 条、CLI 连接文件 2 条；测试后均无残留租约。
- 尚未实测 Windows/MacBook Pro 路径、三机同时运行、断线租约回收和重试耗尽后的恢复。

## 下一步

1. Windows 和 MacBook Pro 按 `docs/WORKER_SETUP_WINDOWS_MACBOOK.md` 安装依赖并安全取得受限账户 `worker.dsn`；各运行 `--limit 10 worker`，核对路径、数据库连接及哈希增长。MacBook Pro 先确认实际 `/Volumes` 挂载路径。
2. 三台机器测试通过后启动长期 `worker`，监控 `distributed_hash.py status`、NAS 磁盘/网络负载和错误数。分配是动态领取，不固定三等份。
3. 当前失败任务最多尝试 5 次后停止重试；需要实现或操作化失败任务的检查、刷新清单和重置流程。源文件新增/删除后也需重新建立清单并迁移更新。
4. 保留原 SQLite、SQLite 备份和 PostgreSQL 基线备份。三机长期运行后还需安排周期性 PostgreSQL 备份，再考虑归档旧数据。
