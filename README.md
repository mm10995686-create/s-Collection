# OpenClaw Skills

一组用于 [OpenClaw](https://github.com/openclaw) 的实用技能集合。

## 技能列表

| 技能 | 触发词 | 说明 |
|------|--------|------|
| [check-domain](./skills/check-domain/) | `/check-domain` | 多节点检测域名在各地区的 HTTP 可达性 |

## 使用方式

将 `skills/` 目录下的技能文件夹复制到 OpenClaw 技能目录：

```bash
cp -r ./check-domain ~/.openclaw/skills/check-domain
```

## 依赖

- Python 3.9+
- Chrome + ChromeDriver（版本需匹配）
- `pip install selenium`

## 免责声明

本项目技能仅限用于合法用途，详见 [LICENSE](./LICENSE)。