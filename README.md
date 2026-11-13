# NAS Deduplicator

运行入口后，程序读取本地 `.env` / `common.env` 中配置的 NAS 挂载路径并打开浏览器操作页。先建立包含路径、大小和修改时间的全量文件清单，再按所选范围计算 SHA-256 哈希；完成后生成 HTML 报告。

程序会先打开三步浏览器操作页：选择范围、选择是否计算 SHA-256、确认后开始。首次可只建立全量文件清单；以后勾选“使用本地文件清单”即可从 SQLite 保存的路径直接计算哈希，无需再次遍历 NAS 目录。部分哈希默认每次最多处理 100 个新文件。群晖回收站目录（如 `#recycle`）默认跳过。命令行无浏览器运行可使用 `--no-open --all`。

部分哈希每次最多处理 100 个尚未建档或已变化的文件，已有且未变化的文件会跳过，因此下次启动可继续处理后续文件。SQLite 用于判断文件是否变化；扫描结束后在报告目录写入 `nas-index.json`，其中包含**已计算哈希**文件的路径、大小、修改时间和哈希值。全量清单保存在 SQLite 的 `inventory` 表。统计文件数需要遍历目录，但不会读取文件内容。

统计和扫描期间可点击“停止扫描并保存”。程序会保留已完成的文件哈希，生成当前索引的树形报告和 JSON；哈希索引每处理约 100 个文件会提交一次。报告的文件树显示已计算哈希的文件。

统计阶段还会把**全部发现的文件**写入 SQLite 的 `inventory` 表，包含完整路径、字节大小和修改时间。页面同步显示已记录文件数及总容量；每 1,000 个文件提交一次。`files` 表只存已计算哈希的文件，因此即使仅做部分哈希，仍可在 `inventory` 中查询全量文件数量和容量。

三台电脑协同计算哈希时，可将清单和已有哈希迁移到 BCBH 独立硬盘卷上运行的 PostgreSQL，再由各电脑领取不同任务。部署与运行步骤见 [三台电脑协同计算哈希](docs/DISTRIBUTED_HASHING.md)。

Windows 和 MacBook Pro 的具体安装、试运行与启动步骤见 [工作节点操作手册](docs/WORKER_SETUP_WINDOWS_MACBOOK.md)。

本轮功能、部署状态和待办见 [2026-11-13 更新记录](docs/UPDATE_2026-11-13.md)。

## 配置

编辑项目根目录下的 `common.env` 或 `.env`，填写以下变量之一：

```dotenv
NAS_ROOT=/path/to/your/mounted/nas
```

也支持平台变量 `LOCAL_NAS_WINDOWS`、`LOCAL_NAS_MACOS`、`LOCAL_NAS_LINUX`。进程中已存在的环境变量优先于文件配置；`NAS_ROOT` 优先于平台变量。仓库中的 `common.env.example` 仅为模板。NAS 必须已经挂载，并能从运行此程序的电脑访问。

HTML 审核区使用 `NAS_DEDUP_DB_HOST`、`NAS_DEDUP_DB_PORT`、`NAS_DEDUP_DSM_URL` 和 `NAS_DEDUP_PG_DATA_DIR` 显示非敏感部署信息；示例值见 `common.env.example`。页面只探测 TCP 端口，不读取或展示数据库密码。

## 运行

```powershell
python nas-deduplicator.py
```

选择“仅保存路径、修改时间和大小”时不会读取文件内容；选择计算哈希时会读取所选文件的完整内容。后续启动时，文件大小和修改时间未变化便会复用已有哈希。如需全部重新计算，可在命令行无浏览器模式运行：

```powershell
python nas-deduplicator.py --no-open --all --force
```

浏览器页面可选择部分或全量哈希。无浏览器命令行模式默认最多处理 100 个新增或变化文件；限量扫描不会清理未处理的索引记录。命令行全量哈希使用：

```powershell
python nas-deduplicator.py --no-open --all
```

可用 `--max-files 20` 调整本机测试数量；`--all` 与 `--max-files` 不能同时使用。

默认在本地浏览器打开操作页，完成后生成 `output/nas-report.html`。可选参数：

- `--root PATH`：本次运行覆盖 NAS 根目录。
- `--report PATH`：更改 HTML 报告输出位置。
- `--database PATH`：更改 SQLite 索引位置。
- `--max-files N`：最多扫描 N 个文件（默认 100）。
- `--all`：扫描根目录下全部文件，用于全量建档。
- `--no-open`：直接在终端执行扫描并生成报告，不打开浏览器操作页。

示例：

```powershell
python nas-deduplicator.py --root "Z:\\Shared" --report "D:\\nas-report.html"
```

## HTML 报告与重复数据校核

报告以可展开/折叠的目录树显示扫描范围内的目录和文件，并列出文件大小、创建日期、修改日期、SHA-256 和重复状态。页面筛选栏提供“显示全量数据”和“显示重复数据”两种视图，也可按文件名或哈希搜索；视图选择与搜索条件可以同时使用。重复按完整 SHA-256 判断，只有哈希完全相同的文件才归为重复。

报告顶部区分全量清单文件数、总容量和已计算哈希文件数，并汇总重复组数和重复文件数。重复文件会标记“重复”，其他文件标记“唯一”。在重复视图中逐项核对文件名、大小、修改时间和 SHA-256；目录树可以展开或折叠。报告还显示 Docker/DSM 主机与 PostgreSQL 端口的探测状态、配置的数据目录和检查时间，供人工核对；端口可达不等于容器健康或数据库身份认证已通过。该报告不会删除或移动文件。无法获取创建日期的平台会显示 `Unavailable`。

本程序只扫描并生成报告，不会删除或移动任何网盘文件。符号链接默认不跟随。无法访问的目录或文件会报告为错误，扫描其余内容后继续。索引和报告中含有文件路径，请保护生成的数据库和 HTML 文件。

## NAS 带宽测试

在确认 NAS 连接速度前，可运行只读测试脚本：

```powershell
python test/test_nas_bandwidth.py
```

脚本从配置的 NAS 路径中自动寻找大文件，默认最多顺序读取 512 MiB，并按 5,000 GB 估算纯数据读取时间。它不会修改 NAS 文件。可用 `--file PATH` 指定一个或多个样本文件，或用 `--limit-mib 1024` 增加采样量。详细说明见 [NAS 扫描与网络带宽说明](docs/NAS_SCAN_AND_NETWORK.md)。

最近一次人工提供的测量结果为：读取 512 MiB 用时 5.06 秒，速度约 101.2 MiB/s（106.1 MB/s）；按十进制 5,000 GB 线性外推，纯读取约 13.1 小时。完整扫描还包括文件遍历、哈希计算及索引写入，因此实际耗时可能更长。

当时 Windows 电脑的 Realtek 2.5GbE 网卡协商速率为 1 Gbps，路由器报告为 ASUS AC66。此连接下测得约 106 MB/s 与千兆链路相符。AC66 有不同后缀版本；常见 RT-AC66U/B1 的 LAN 口规格为 1GbE，建议按机身完整型号核对。即使换成带 2.5GbE 口的路由器，若要单客户端使用 NAS 两个网口，还需要 NAS 型号支持并启用 SMB3 Multichannel；请先阅读网络说明再调整配置。

## 技术信息

- Python 3.10+
- 运行依赖：Python 标准库，无需安装第三方包
- SQLite 索引：默认 `.nas-deduplicator.sqlite3`
- 分布式工作节点依赖：`requirements-distributed.txt`；连接 PostgreSQL `192.168.1.192:15432`
- 日志：`logs/nas-deduplicator.log`

`logging_config.py` 为项目通用日志实现，入口仅使用其公开接口。
