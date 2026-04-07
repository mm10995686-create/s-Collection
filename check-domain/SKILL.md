---
name: check-domain
description: 检测域名在国内各地区的 HTTP 可达性，支持监控列表管理、远程配置同步和定时巡检。触发场景：用户说"检测域名"、"域名可达性"、"/check-domain" 时使用。
allowed-tools: Bash(npx:*) Bash(npm:*) Bash(pkill:*) Bash(pgrep:*)
---

# 域名可达性检测

> **使用声明**：本工具仅用于站长检测**自有域名**在各地区的网络可达性，请勿用于非授权域名的批量扫描。检测数据由第三方公开平台提供，请合理控制调用频率，遵守对应平台服务条款。

## 依赖安装

```bash
cd scripts
npm install
npx playwright install chromium
```

## 快速开始

```bash
# 一次性检测单个域名（无需加入监控列表）
npx ts-node scripts/check_domain.ts example.com

# 添加域名到监控列表
npx ts-node scripts/check_domain.ts add example.com

# 执行检测
npx ts-node scripts/check_domain.ts run

# 查看监控列表
npx ts-node scripts/check_domain.ts list
```

## 常用命令

### 监控列表管理

```bash
# 批量添加/删除
npx ts-node scripts/check_domain.ts add example1.com example2.com
npx ts-node scripts/check_domain.ts remove example.com

# 从文件批量添加/删除（每行一个域名，# 开头为注释）
npx ts-node scripts/check_domain.ts add -f domains.txt
npx ts-node scripts/check_domain.ts remove -f domains.txt
```

### 远程 JSON 同步（sync）

适用于从远程配置中心拉取域名列表。JSON 格式为对象，**键为配置项标识**（如 `"{share.xxx}"`），**值为域名/主机**：

```json
{ "{share.sir}": "ca0.eohdbxs.cc", "{share.zpc}": "b7e.ixgklwa.cc" }
```

同步时只写入 **`synced_map.json`**：完整保留 `key → 域名` 列表（供 `run --synced` 在输出里标注 key）。

**非 JSON / 手工列表**：可用 `run -f 文件.txt`，每行一个域名；若需带 key，使用**制表符**分隔：`{share.xxx}` + Tab + `域名`（与 Excel 粘贴、脚本导出兼容）。

```bash
# 首次同步，指定远程地址
npx ts-node scripts/check_domain.ts sync https://example.com/config.json

# 同步并指定分批大小（检测时每批 20 个域名）
npx ts-node scripts/check_domain.ts sync https://example.com/config.json --batch-size 20

# 复用上次地址（无需重复填 URL）
npx ts-node scripts/check_domain.ts sync

# 强制刷新（忽略 10 分钟冷却）
npx ts-node scripts/check_domain.ts sync --force
```

`--batch-size` 会记录到 `sync_meta.json`，`run --synced` 时自动读取并分批检测，无需重复指定。

内置 10 分钟冷却防重复拉取。

**HTTP 拉取失败**：自动间隔 **5 秒**重试，**最多共 4 次**（首次 + 失败后最多 3 次）；仍失败则退出并打印错误。

**推荐搭配定时器**：在 OpenClaw 中说"每 10 分钟执行一次 sync"即可。

### 执行检测

```bash
# 检测监控列表中所有域名
npx ts-node scripts/check_domain.ts run

# 进程中断后，从断点续跑（跳过日志中已完成的域名，ERROR 会重试）
npx ts-node scripts/check_domain.ts run --resume
npx ts-node scripts/check_domain.ts run --synced --resume

# 检测 sync 拉取的远程配置域名（自动按 sync 时记录的批次大小分批）
npx ts-node scripts/check_domain.ts run --synced

# 检测指定文件中的域名
npx ts-node scripts/check_domain.ts run -f domains.txt

# 控制并发数（默认 3，最大 5）
npx ts-node scripts/check_domain.ts run -c 5

# 显示详情 + 海外节点
npx ts-node scripts/check_domain.ts run -v --overseas

# 每完成 N 个域名打印一次进度（默认 10；设为 0 关闭）
npx ts-node scripts/check_domain.ts run --synced --progress-every 10

# 临时覆盖批次大小和批次间隔（单位：秒）
npx ts-node scripts/check_domain.ts run --synced --batch-size 10 --batch-delay 60
```

### 大批量检测时的进度汇报

脚本已内置 **`--progress-every N`（默认 10）**：每完成 N 个域名会在终端打印一行 `📍 进度: 已完成 m/总数`，远程配置几百个域名时不会长时间无输出。设为 `0` 可关闭。

**OpenClaw Agent** 仍可在适当时机用自然语言向用户补充摘要（例如本段异常域名简称），与脚本进度互补。

### 远程 sync 结果的输出格式（含 share key）

使用 `run --synced` 且存在 `synced_map.json` 时，每条检测与汇总中会标注 **`{share.xxx} → 域名`**，便于与配置中心里的 key 对照，例如：

`{share.yd_clsq_zz} → example.com — 正常访问 …`

### 一次性检测（不写入监控列表）

```bash
# 直接传域名
npx ts-node scripts/check_domain.ts example.com -v --overseas --threshold 0.5

# 从文件读取域名
npx ts-node scripts/check_domain.ts -f domains.txt -v

# 文件 + 命令行域名合并
npx ts-node scripts/check_domain.ts extra.com -f domains.txt
```

## 参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `add <域名...>` | | 批量添加域名到监控列表 |
| `remove <域名...>` | | 批量从监控列表删除域名 |
| `list` | | 查看监控列表 |
| `sync <url>` | | 从远程 JSON 拉取域名并缓存到本地 |
| `run` | | 检测监控列表中所有域名 |
| `--file` | `-f` | `run` / 一次性检测：每行一个域名，或「key + Tab + 域名」；`add`/`remove` 仍为每行一个域名 |
| `--synced` | `-s` | `run` 时使用 `synced_map.json`（须先 `sync`） |
| `--force` | | `sync` 时忽略 10 分钟冷却强制重拉 |
| `--batch-size` | `-B` | `sync`：记录分批大小；`run`：每批域名数（0 = 不分批）；`run --synced` 自动继承 sync 时的设定 |
| `--resume` | `-r` | `run` 时跳过日志已完成域名，从断点续跑（ERROR 会重试） |
| `--batch-delay` | | `run` 批次间等待秒数（默认 30s） |
| `--concurrency` | `-c` | 并发数（默认 3，最大 5） |
| `--verbose` | `-v` | 显示各节点 IP、HTTP 状态码 |
| `--overseas` | `-o` | 包含港澳台、海外节点 |
| `--threshold` | | 异常判定阈值，默认 `0.7` |
| `--progress-every` | | `run` / 一次性检测：每完成 N 个域名打印进度（默认 `10`，`0` 关闭） |
| `--platforms` | | 启用的检测平台，逗号分隔，默认 `itdog,chinaz`（17ce 不稳定默认关闭）；启用三平台：`--platforms itdog,17ce,chinaz` |

## 日志格式

日志文件 `check.log` 实时追加，每完成一个域名立即写入一行，格式如下：

```
========================================================
开始 2026-04-03 19:00:00，共 300 个域名
========================================================
  OK      [1/300] [itdog] domain5  (100.0% 142/142)
  BLOCKED [2/300] [17ce] domain1  (28.0% 14/50)
  ERROR   [3/300] [chinaz] domain3: 检测超时
  [进度] 2026-04-03 19:05:00  已完成 10/300，发现 1 个异常
  ...
========================================================
完成 2026-04-03 19:30:00，共 300 个，异常 5 个
========================================================
```

- `[完成序号/总数]` 标注第几个完成（并发时按实际完成顺序递增，便于 OpenClaw 追踪进度）
- 每条结果带 `[itdog]` / `[17ce]` / `[chinaz]` 平台标签
- 每完成 10 个写一行带时间戳的进度行（stdout 同步输出）
- OpenClaw 可通过 `tail -f check.log` 实时观察进度

## 文件路径

- `~/.openclaw/data/check-domain/watchlist.json` — 监控列表
- `~/.openclaw/data/check-domain/check.log` — 检测日志（实时追加）
- `~/.openclaw/data/check-domain/synced_map.json` — **sync 拉取的 key→域名映射**（`run --synced` 唯一数据源）
- `~/.openclaw/data/check-domain/sync_meta.json` — sync 元信息（地址、时间、数量、分批大小）

## 停止检测

用户说"停止"、"取消"、"别查了"时，执行：

```bash
# 停止主进程（check_domain.ts）
pkill -f "check_domain.ts"

# 停止所有 probe 子进程（probe.mjs 等，进程名均为 check-domain）
pkill check-domain
```

两条都执行，若进程已退出则忽略错误。停止后告知用户已完成的批次数和域名数（从 `check.log` 末尾读取）。

## 定时巡检

在 OpenClaw 中直接说以下两句话即可设置定时任务：

- "**每 10 分钟同步一次域名列表**" → 定时执行 `sync <url>`
- "**每 2 小时检测一次域名**" → 定时执行 `run --synced`

## 并发说明

- **平台可配置**：`--platforms itdog,chinaz` 只用指定平台，workers 按平台数均分
  - 默认两平台（itdog+chinaz）：`-c 2` → itdog×1 + chinaz×1；`-c 4` → itdog×2 + chinaz×2
  - 启用三平台：`--platforms itdog,17ce,chinaz -c 3` → itdog×1 + 17ce×1 + chinaz×1
- **互相兜底**：任意已启用平台初始化失败，其余已启用平台自动均摊接管，检测不中断
- **单浏览器多 Page**：三平台各一个 BrowserContext，共用同一 Chromium 进程
- itdog.cn 的访问验证（高峰期「进入网站」按钮）只需过一次，Context 内 Cookie 共享
- 17ce.com 只检测大陆节点（电信 / 联通 / 移动 / 铁通）
- chinaz（tool.chinaz.com/speedtest）国内多节点测速，约 50 个节点；`code=2` 消息为检测结束标志
- 单域名或 `-c 1` 只用 itdog

## 异常判定

- 成功率 < 70% 提示可能存在访问异常
- 退出码 `1` 表示存在异常节点（可用于 CI/定时巡检）

## 对比 Python 旧版的优势

| 维度 | Python 旧版 | 本版（TypeScript + Playwright） |
|------|-------------|--------------------------------|
| **检测平台** | 单一平台（itdog） | 三平台并发（itdog + 17ce + chinaz），互相兜底 |
| **节点数量** | ~50 个节点 | 三平台合计可达 350+ 节点（itdog 142、17ce 150+、chinaz 50+） |
| **并发模式** | 串行，一次一个域名 | 多 worker 并发，同时检测多个域名 |
| **平台容错** | 平台挂掉则整批失败 | 任一平台初始化失败，其余自动接管，检测不中断 |
| **日志** | 全部完成后一次性写入 | 每域名完成立即实时追加，带平台标签和进度行 |
| **进度可观测** | 无 | 每 10 个域名打印进度（stdout + 日志），OpenClaw 可实时读取 |
| **大批量分批** | 不支持 | `--batch-size` + `--batch-delay` 自动分批，避免平台限流 |
| **配置中心集成** | 手动维护列表 | `sync <url>` 从远程 JSON 拉取，10 分钟冷却防重复，带 key 标注 |
| **反检测** | 无 | 注入 WebSocket 代理拦截结果，`webdriver` 属性屏蔽 |