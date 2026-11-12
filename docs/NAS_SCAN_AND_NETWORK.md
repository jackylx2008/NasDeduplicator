# NAS 扫描与网络带宽说明

本文记录当前扫描方式、带宽测试结果，以及通过 SMB 使用 NAS 多网口时需要检查的条件。

## 扫描量与预计耗时

首轮索引需要读取每个文件的完整内容来计算 SHA-256。若数据总量为 5,000 GB，粗略纯读取时长可按下式估算：

```text
预计小时数 = 5,000,000 MB ÷ 实测 MB/s ÷ 3,600
```

最近一次用户提供的实测数据：

- 样本读取量：512 MiB
- 读取时间：5.06 秒
- 顺序读取速度：101.2 MiB/s，约 106.1 MB/s
- 5,000 GB 纯读取估算：约 13.1 小时
- 搜索样本时检查了 100 个目录，用时约 1 秒

这是短样本估算，不是全盘扫描实测。大量小文件、目录元数据访问、NAS 磁盘阵列、文件哈希、SQLite 写入及网络波动都会影响实际总耗时。后续程序会根据文件大小和修改时间复用已有哈希，减少重复读取。

## 重测 NAS 读取速度

在项目根目录运行：

```powershell
python test/test_nas_bandwidth.py
```

脚本只读取 NAS 上已有文件，不会写入或修改文件。默认最多读取 512 MiB；可以增加样本量或指定文件：

```powershell
python test/test_nas_bandwidth.py --limit-mib 2048
python test/test_nas_bandwidth.py --file "\\BCBH\三维扫描存储\sample.bin"
```

自动发现文件会限制目录遍历范围；如果没有找到足够大的文件，使用 `--file` 指定较大的代表性文件。测试结果是顺序读取速度，不等于小文件扫描速度。

## 查看电脑端链路速率

在 Windows PowerShell 中查看网卡当前协商速率：

```powershell
Get-NetAdapter | Format-Table Name, Status, LinkSpeed, InterfaceDescription -Auto
```

本次测量电脑的 Realtek Gaming 2.5GbE 网卡状态为 `Up`，但 `LinkSpeed` 为 `1 Gbps`。这说明网卡本身支持 2.5GbE，但当前连接协商在 1 Gbps；网卡速率由两端端口、线缆和自动协商共同决定。约 106 MB/s 的读取结果符合千兆以太网的实际吞吐区间。

## 两个 NAS 网口与单台 Windows 电脑

NAS 的两个 DSM 管理地址都能打开，说明这两个地址对应的 Web 管理服务可达；这本身不能证明 SMB 文件流量已通过两个网口传输。

单个 SMB 连接通常只使用一条路径。要让单台 Windows 电脑的一个 SMB 会话并行使用多个网络路径，需要 NAS 与 Windows 客户端均支持 SMB 3，并且 NAS 型号和 DSM/SMB Service 版本支持 SMB3 Multichannel。群晖给出的支持条件与型号限制见[官方 DSM 文件服务规格](https://www.synology.com/en-global/dsm/7.4/software_spec/file_services_protocols)。若支持，可在 DSM 的“控制面板 → 文件服务 → SMB → 高级设置 → 其他”中查找并启用 SMB3 Multichannel。

Windows 客户端可用以下命令查看 SMB 多通道设置与实际连接：

```powershell
Get-SmbClientConfiguration | Select-Object EnableMultiChannel
Get-SmbMultichannelConnection
```

第二条命令应在实际读取 SMB 文件时运行。Windows 连接查看可参考[微软 SMB Multichannel 指南](https://learn.microsoft.com/en-us/windows-server/storage/storage-spaces/manage-smb-multichannel)。群晖提醒，实际吞吐还取决于客户端总带宽、NAS 网卡速率、存储性能与网络拓扑；增加端口不保证速度按端口数量线性增长。[群晖多通道排查说明](https://kb.synology.com/en-us/DSM/tutorial/troubleshoot_smb3_multichannel_issues)

## 当前路由器与升级思路

当前记录的路由器型号为 ASUS AC66，但还没有核实机身完整后缀。华硕 RT-AC66U B1 官方规格标注 WAN 和 LAN 均为 10/100/1000 Base-T；若设备确为该型号，电脑连接该路由器的有线链路最高为 1 Gbps。[华硕 RT-AC66U B1 官方规格](https://www.asus.com/networking-iot-servers/wifi-routers/asus-wifi-routers/rt-ac66u-b1/techspec/)

若目标是让单台电脑更快读取 NAS，可考虑以下路径：

1. 核实 NAS 型号是否支持 SMB3 Multichannel，并先确认两条 NAS 接口都以预期速率连接。
2. 如果现有路由器没有 2.5GbE LAN，可将电脑与 NAS 接到同一台多千兆交换机，再将交换机上联现有路由器。这样本地电脑与 NAS 的流量可以在交换机内转发。
3. 或更换有 2.5GbE LAN 端口的路由器。连接电脑的端口需要配置为 LAN；要同时连接多个 NAS 端口，还要确认 LAN 端口数量和交换能力足够。
4. Link Aggregation 与 SMB3 Multichannel 是不同机制。群晖支持的 SMB3 Multichannel 与 Link Aggregation 不能并用；链路聚合通常更适合提高多客户端总吞吐和故障切换，不应默认认为它会让单条 SMB 连接翻倍。

不要仅根据 DSM 管理页面能通过两个 IP 打开，就认定两个 SMB 数据通道已经生效。调整前请核实 NAS 完整型号、DSM 版本、Windows 端 `LinkSpeed`，并在传输过程中检查 `Get-SmbMultichannelConnection`。
