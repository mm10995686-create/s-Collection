---
name: highlight-clip
description: 通过帧间运动量检测（默认）或本地 LLaVA 视觉模型，自动分析 M3U8 视频并剪辑出最精彩的高光片段，用于引流或短视频推广。触发场景：用户说"剪辑高光"、"找精彩片段"、"提取亮点"、"/highlight-clip" 时使用。
allowed-tools: Bash(python3:*) Bash(pip3:*) Bash(pip:*) Bash(ffmpeg:*) Bash(ffprobe:*) Bash(ollama:*)
---

# 高光时刻自动剪辑

## 依赖安装

```bash
# 系统依赖
brew install ffmpeg

# Python 依赖
pip3 install -r scripts/requirements.txt

# 本地视觉模型（可选，用于语义描述）
brew install ollama          # 或官网下载 dmg
ollama pull llava            # 7B，约 4.7GB
ollama serve                 # 启动服务
```

## 快速开始

```bash
# 基本用法：motion 评分，提取 Top 5 高光，合并输出
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --merge

# 加上语义描述（motion 评分 + LLaVA 描述高光帧，多约 1 分钟）
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --merge --describe

# 仅分析，不剪辑
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --no-clip
```

## 完整参数

```
python3 scripts/highlight_clip.py <m3u8_url> [选项]

评分模式
  --mode motion         帧间像素差（默认，最快，无内容限制）
  --mode llava          LLaVA 全量语义评分（慢，7B 区分度差）
  --mode hybrid         motion × 0.6 + LLaVA × 0.4

采样
  -i, --interval <秒>   帧采样间隔（默认 3，长视频自动加大）
  --max-frames <数>     最多采样帧数（默认 150）

高光识别
  -t, --threshold <0-100>  分数阈值（默认 60，自动自适应）
  -n, --count <数>         输出片段数（默认 5）

片段
  -d, --clip-duration <秒> 每片段时长（默认 20）
  -m, --merge              合并为 highlights.mp4
  --describe               motion 模式下用 LLaVA 对高光峰值帧补充描述
  --no-clip                仅分析，不剪辑

模型
  --model <名>          Ollama 模型名（默认 llava）
  -c, --concurrency <数> LLaVA 并发数（默认 2）

其他
  -o, --output-dir <路径>  输出目录（默认自动创建）
  -v, --verbose            显示详细日志
```

## 工作流程

```
M3U8 URL
  ├─ ffprobe 获取时长
  ├─ ffmpeg 提取帧（scale=1280, fps=1/interval）
  ├─ 运动量计算（帧间像素差，归一化 0-100）
  ├─ [可选] LLaVA 对 Top N 峰值帧补充描述（--describe）
  ├─ 移动平均 + 峰值检测 → 高光时间段
  ├─ ffmpeg 精准裁切各片段
  └─ [可选] ffmpeg concat 合并
```

## 输出结构

```
~/.openclaw/data/highlight-clip/<session-id>/
├── frames/           提取的视频帧（JPG）
├── analysis.json     每帧评分结果
├── highlights.json   高光片段信息
├── clip_01.mp4       第 1 高光片段
├── ...
└── highlights.mp4    合并版（--merge）
```

## 速度参考（16GB 内存 Mac，17 分钟视频）

| 阶段 | 耗时 |
|------|------|
| 提帧（149 帧） | 5-8 分钟 |
| motion 运动量计算 | < 30 秒 |
| LLaVA 描述 5 帧（--describe） | 约 1 分钟 |
| 剪辑 5 个片段 | 3-5 分钟 |

---

## 局限与解决方案

### 1. motion 模式无语义

**问题**：只看像素变化量，不理解内容，镜头切换等也会得高分。

**缓解**：加 `--describe` 让 LLaVA 描述峰值帧内容，方便人工筛选。

---

### 2. LLaVA 7B 评分区分度差

**问题**：LLaVA 7B 对所有帧打分偏高（85-95），无法区分普通和精彩时刻，不适合用 `--mode llava`。

**解决**：换更大的模型。

| 模型 | 显存 | 质量 |
|------|------|------|
| `llava:7b`（当前） | 8GB | ⭐⭐ 区分度差 |
| `llava:13b` | 16GB | ⭐⭐⭐⭐ 明显提升 |
| `llava:34b` | 40GB | ⭐⭐⭐⭐⭐ 接近 GPT-4V |

本机 16GB 内存不够跑 13B，需要 **租 GPU 服务器**。

---

### 3. 成人内容只能自部署

**问题**：GPT-4o / Claude / Gemini 及国内主流 API（智谱、通义）均拒绝分析成人内容。本地 Ollama 是唯一无审核方案。

**最优解：租 GPU 云服务器，按小时计费**

| 平台 | 推荐配置 | 可跑模型 | 费用 |
|------|---------|---------|------|
| AutoDL | RTX 4090 (24G) | llava:13b | ~¥2/小时 |
| 矩池云 | A100 (40G) | llava:34b | ~¥10/小时 |
| 飞行云 | RTX 3090 (24G) | llava:13b | ~¥2/小时 |

处理一个 17 分钟视频只需约 10 分钟，实际花费 **< ¥1**。

**接入方式**：服务器装好 Ollama 后，设置环境变量即可：
```bash
export OLLAMA_HOST=http://服务器IP:11434
python3 scripts/highlight_clip.py <url> --mode llava --model llava:13b
```

---

### 4. 国内无法直连境外 API

**问题**：Anthropic / OpenAI / Google 对中国大陆 IP 返回 403。

**解决**：VPN 全局代理，或设置中转地址：
```bash
export ANTHROPIC_BASE_URL=https://你的中转地址/v1
```

---

## 公司部署推荐

### 最佳效果配置（大模型 + 语义评分）

当公司有 GPU 服务器时，使用 `--mode llava` 让大模型直接参与评分，效果远好于默认的运动量检测。

**服务器端（一次性配置）：**
```bash
# 安装 Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 下载大模型（选其一）
ollama pull llava:13b   # 推荐，16GB 显存，效果好
ollama pull llava:34b   # 最佳，40GB 显存，接近 GPT-4V

# 启动服务（允许外部访问）
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

**员工本机（每次使用前设置一次）：**
```bash
export OLLAMA_HOST=http://公司服务器IP:11434
```

**运行命令（大模型模式）：**
```bash
# 用大模型直接评分（区分度好，描述准确）
python3 scripts/highlight_clip.py <m3u8_url> \
  --mode llava --model llava:13b \
  --count 5 --merge

# 最高质量（34B 模型）
python3 scripts/highlight_clip.py <m3u8_url> \
  --mode llava --model llava:34b \
  --count 5 --merge
```

### 各配置效果对比

| 配置 | 命令 | 区分度 | 描述质量 | 适用场景 |
|------|------|--------|---------|---------|
| 纯运动量（无模型） | 默认 | ✅ 好 | ❌ 无 | 快速出片，不需要描述 |
| 运动量 + 7B 描述 | `--describe` | ✅ 好 | ⭐⭐ 基本可读 | 本机，需要了解片段内容 |
| 7B 语义评分 | `--mode llava` | ❌ 差 | ⭐⭐ | 不推荐 |
| **13B 语义评分** | `--mode llava --model llava:13b` | ✅ 好 | ⭐⭐⭐⭐ | **公司推荐** |
| **34B 语义评分** | `--mode llava --model llava:34b` | ✅ 最好 | ⭐⭐⭐⭐⭐ | **最佳效果** |

### 服务器选型参考

| 平台 | 配置 | 可跑模型 | 费用 |
|------|------|---------|------|
| AutoDL | RTX 4090 (24G) | llava:13b | ~¥2/小时 |
| 矩池云 | A100 (40G) | llava:34b | ~¥10/小时 |
| 自购 | RTX 4090 整机 | llava:13b | ~¥2-3 万（一次性）|

> 业务量小先按小时租，跑量大再考虑包月或自购。

---

## 常见问题

**Q: Ollama 连不上？**
```bash
ollama serve        # 手动启动
ollama list         # 查看已下载模型
```

**Q: 提帧太慢？**
- 加大采样间隔：`-i 10`
- 减少帧数：`--max-frames 80`

**Q: 片段选得不准？**
- 降低阈值：`-t 40`（更多候选）
- 缩小采样间隔：`-i 2`（更密集，更精准）
- 换大模型：租服务器跑 llava:13b
