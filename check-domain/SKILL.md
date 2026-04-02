---
name: check-domain
description: 通过第三方公开检测平台多节点验证域名在各地区的 HTTP 可达性。使用场景：(1) 检测自有域名在国内/海外的访问成功率，(2) 管理域名监控列表，(3) 定时巡检域名可用性，(4) 从远程 JSON 配置同步域名列表。触发词：/check-domain、检测域名、域名可达性
---

# 域名可达性检测

> **使用声明**：本工具仅用于站长检测**自有域名**在各地区的网络可达性，请勿用于非授权域名的批量扫描。检测数据由第三方公开平台提供，请合理控制调用频率，遵守对应平台服务条款。

## 依赖安装

```bash
pip install selenium
# 需本机安装 Chrome + ChromeDriver（版本需匹配）
```

## 快速开始

```bash
# 添加域名到监控列表
python3 scripts/check_domain.py add example.com

# 执行检测
python3 scripts/check_domain.py run

# 查看监控列表
python3 scripts/check_domain.py list
```

## 常用命令

### 监控列表管理

```bash
# 批量添加/删除
python3 scripts/check_domain.py add example1.com example2.com
python3 scripts/check_domain.py remove example.com

# 从文件批量添加/删除（每行一个域名，# 开头为注释）
python3 scripts/check_domain.py add -f domains.txt
python3 scripts/check_domain.py remove -f domains.txt
```

### 远程 JSON 同步（sync）

适用于从远程配置中心拉取域名列表，JSON 格式为 `{"key": "domain", ...}`，所有值被提取为域名。

```bash
# 首次同步，指定远程地址
python3 scripts/check_domain.py sync https://example.com/config.json

# 复用上次地址（无需重复填 URL）
python3 scripts/check_domain.py sync

# 强制刷新（忽略 10 分钟冷却）
python3 scripts/check_domain.py sync --force
```

同步结果写入 `~/.openclaw/data/check-domain/synced_domains.txt`，内置 10 分钟冷却防重复拉取。

**推荐搭配定时器**：在 OpenClaw 中说"每 10 分钟执行一次 sync"即可。

### 执行检测

```bash
# 检测监控列表中所有域名
python3 scripts/check_domain.py run

# 检测 sync 拉取的远程配置域名
python3 scripts/check_domain.py run --synced

# 检测指定文件中的域名
python3 scripts/check_domain.py run -f domains.txt

# 控制并发数（默认 3，最大 5）
python3 scripts/check_domain.py run -c 5

# 显示详情 + 海外节点
python3 scripts/check_domain.py run -v --overseas
```

### 一次性检测（不写入监控列表）

```bash
# 直接传域名
python3 scripts/check_domain.py example.com -v --overseas --threshold 0.5

# 从文件读取域名
python3 scripts/check_domain.py -f domains.txt -v

# 文件 + 命令行域名合并
python3 scripts/check_domain.py extra.com -f domains.txt
```

## 参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `add <域名...>` | | 批量添加域名到监控列表 |
| `remove <域名...>` | | 批量从监控列表删除域名 |
| `list` | | 查看监控列表 |
| `sync <url>` | | 从远程 JSON 拉取域名并缓存到本地 |
| `run` | | 检测监控列表中所有域名 |
| `--file` | `-f` | 从文件读取域名（每行一个，支持 add/remove/run/一次性检测） |
| `--synced` | `-s` | `run` 时使用 sync 缓存的本地文件 |
| `--force` | | `sync` 时忽略 10 分钟冷却强制重拉 |
| `--concurrency` | `-c` | 并发数（默认 3，最大 5） |
| `--verbose` | `-v` | 显示各节点 IP、HTTP 状态码 |
| `--overseas` | `-o` | 包含港澳台、海外节点 |
| `--threshold` | | 异常判定阈值，默认 `0.7` |

## 文件路径

- `~/.openclaw/data/check-domain/watchlist.json` — 监控列表
- `~/.openclaw/data/check-domain/check.log` — 检测日志
- `~/.openclaw/data/check-domain/synced_domains.txt` — sync 拉取的域名缓存
- `~/.openclaw/data/check-domain/sync_meta.json` — sync 元信息（地址、时间、数量）

## 定时巡检

在 OpenClaw 中直接说以下两句话即可设置定时任务：

- "**每 10 分钟同步一次域名列表**" → 定时执行 `sync <url>`
- "**每 2 小时检测一次域名**" → 定时执行 `run --synced`

## 并发说明

- 多域名并发跑独立 Chrome 实例，各实例错峰 3-5 秒启动
- 并发输出带域名前缀区分，汇总结果保持原始顺序
- 单域名或 `-c 1` 自动退化为串行

## 异常判定

- 成功率 < 70% 提示可能存在访问异常
- 退出码 `1` 表示存在异常节点（可用于 CI/定时巡检）