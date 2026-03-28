---
name: check-domain
description: 通过第三方公开检测平台多节点验证域名在各地区的 HTTP 可达性。使用场景：(1) 检测自有域名在国内/海外的访问成功率，(2) 管理域名监控列表，(3) 定时巡检域名可用性。触发词：/check-domain、检测域名、域名可达性
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
```

### 执行检测

```bash
# 检测监控列表中所有域名
python3 scripts/check_domain.py run

# 控制并发数（默认 3，最大 5）
python3 scripts/check_domain.py run -c 3

# 显示详情 + 海外节点
python3 scripts/check_domain.py run -v --overseas
```

### 一次性检测（不写入监控列表）

```bash
python3 scripts/check_domain.py example.com -v --overseas --threshold 0.5
```

## 参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `add <域名...>` | | 批量添加域名到监控列表 |
| `remove <域名...>` | | 批量从监控列表删除域名 |
| `list` | | 查看监控列表 |
| `run` | | 检测监控列表中所有域名 |
| `--concurrency` | `-c` | 并发数（默认 3，最大 5） |
| `--verbose` | `-v` | 显示各节点 IP、HTTP 状态码 |
| `--overseas` | `-o` | 包含港澳台、海外节点 |
| `--threshold` | | 异常判定阈值，默认 `0.7` |

## 文件路径

- `~/.openclaw/data/check-domain/watchlist.json` — 监控列表
- `~/.openclaw/data/check-domain/check.log` — 检测日志

## 定时巡检

在 OpenClaw 中直接说"**每 2 小时检测一次域名**"即可设置定时任务。

## 并发说明

- 多域名并发跑独立 Chrome 实例，各实例错峰 3-5 秒启动
- 并发输出带域名前缀区分，汇总结果保持原始顺序
- 单域名或 `-c 1` 自动退化为串行

## 异常判定

- 成功率 < 70% 提示可能存在访问异常
- 退出码 `1` 表示存在异常节点（可用于 CI/定时巡检）