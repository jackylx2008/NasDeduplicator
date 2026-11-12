# NAS Deduplicator

运行入口后，程序会自动读取本地 `.env` / `common.env` 中配置的 NAS 挂载路径，递归扫描文件、建立 SHA-256 索引，生成并打开 HTML 文件树报告。

## 配置

编辑项目根目录下的 `common.env` 或 `.env`，填写以下变量之一：

```dotenv
NAS_ROOT=/path/to/your/mounted/nas
```

也支持平台变量 `LOCAL_NAS_WINDOWS`、`LOCAL_NAS_MACOS`、`LOCAL_NAS_LINUX`。进程中已存在的环境变量优先于文件配置；`NAS_ROOT` 优先于平台变量。仓库中的 `common.env.example` 仅为模板。NAS 必须已经挂载，并能从运行此程序的电脑访问。

## 运行

```powershell
python nas-deduplicator.py
```

首次扫描会读取每个文件内容来计算哈希，耗时取决于网盘容量和连接速度。后续启动时，如果文件大小和修改时间未变化，会复用已有哈希；如需全部重新计算：

```powershell
python nas-deduplicator.py --force
```

为避免在本机对 5,000 GB 以上的数据做全量读取，默认仅按路径顺序扫描前 100 个文件，并且报告只显示本次处理的文件。限量扫描不会清理索引中未扫描的记录。确认测试完成后，在用于全量建档的电脑上运行：

```powershell
python nas-deduplicator.py --all
```

可用 `--max-files 20` 调整本机测试数量；`--all` 与 `--max-files` 不能同时使用。

默认生成 `output/nas-report.html` 并在默认浏览器中打开。可选参数：

- `--root PATH`：本次运行覆盖 NAS 根目录。
- `--report PATH`：更改 HTML 报告输出位置。
- `--database PATH`：更改 SQLite 索引位置。
- `--max-files N`：最多扫描 N 个文件（默认 100）。
- `--all`：扫描根目录下全部文件，用于全量建档。
- `--no-open`：生成报告但不自动打开浏览器。

示例：

```powershell
python nas-deduplicator.py --root "Z:\\Shared" --report "D:\\nas-report.html"
```

## HTML 报告与重复数据校核

报告以可展开/折叠的目录树显示扫描范围内的目录和文件，并列出文件大小、创建日期、修改日期、SHA-256 和重复状态。页面筛选栏提供“显示全量数据”和“显示重复数据”两种视图，也可按文件名或哈希搜索；视图选择与搜索条件可以同时使用。重复按完整 SHA-256 判断，只有哈希完全相同的文件才归为重复。

报告顶部汇总文件数、目录数、重复组数和重复文件数。重复文件会标记“重复”，其他文件标记“唯一”。在重复视图中逐项核对文件名、大小、修改时间、完整路径和 SHA-256；目录树可以展开或折叠。该报告用于人工校核，不会删除或移动文件。无法获取创建日期的平台会显示 `Unavailable`。

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
- 日志：`logs/nas-deduplicator.log`

`logging_config.py` 为项目通用日志实现，入口仅使用其公开接口。
