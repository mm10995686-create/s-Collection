---
name: highlight-clip
description: 通过帧间运动量检测（默认）或 CLIP 视觉语义模型，自动分析 M3U8 视频并剪辑出最精彩的高光片段，用于引流或短视频推广。触发场景：用户说"剪辑高光"、"找精彩片段"、"提取亮点"、"/highlight-clip" 时使用。
allowed-tools: Bash(python3:*) Bash(pip3:*) Bash(pip:*) Bash(ffmpeg:*) Bash(ffprobe:*) Bash(ollama:*)
---

# 高光时刻自动剪辑

## 依赖安装

```bash
# 系统依赖
brew install ffmpeg

# Python 依赖
pip3 install -r scripts/requirements.txt

# 本地视觉模型（可选，用于 --describe 语义描述）
brew install ollama          # 或官网下载 dmg
ollama pull llava            # 7B，约 4.7GB
ollama serve                 # 启动服务
```

## 快速开始

```bash
# 基本用法：motion 评分，提取 Top 5 高光，合并输出
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --merge

# CLIP 语义评分（CPU 即可，比 motion 更智能）
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --mode clip --merge

# 混合模式（motion + CLIP）
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --mode hybrid --merge

# 加上语义描述（任意模式 + LLaVA 描述高光帧，需要 Ollama）
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --mode clip --describe --merge

# 仅分析，不剪辑
python3 scripts/highlight_clip.py https://example.com/video.m3u8 --no-clip
```

## 完整参数

```
python3 scripts/highlight_clip.py <m3u8_url> [选项]

评分模式
  --mode motion         帧间像素差（默认，最快，无内容限制）
  --mode clip           CLIP 语义评分（CPU 即可，区分度好）
  --mode hybrid         motion × 0.6 + CLIP × 0.4

采样
  -i, --interval <秒>   帧采样间隔（默认 3，长视频自动加大）
  --max-frames <数>     最多采样帧数（默认 150）

高光识别
  -t, --threshold <0-100>  分数阈值（默认 60，自动自适应）
  -n, --count <数>         输出片段数（默认 5）

片段
  -d, --clip-duration <秒> 每片段时长（默认 20）
  -m, --merge              合并为 highlights.mp4
  --describe               用 LLaVA 对高光峰值帧补充描述（需要 Ollama）
  --no-clip                仅分析，不剪辑

下载
  --download-concurrency <数>  M3U8 分片并发下载数（默认 16）

CLIP 模型
  --clip-model <名>        CLIP 模型架构（默认 ViT-B-32）
  --clip-pretrained <名>   预训练权重（默认 laion2b_s34b_b79k）

LLaVA 模型（仅 --describe 用）
  --model <名>          Ollama 模型名（默认 llava）
  -c, --concurrency <数> LLaVA 描述并发数（默认 2）

其他
  -o, --output-dir <路径>  输出目录（默认自动创建）
  -v, --verbose            显示详细日志
```

## 工作流程

```
M3U8 URL
  ├─ ffprobe 获取时长
  ├─ 并发下载 M3U8 分片 → ffmpeg 合并为本地 video.mp4（自动检测，非 M3U8 跳过）
  ├─ ffmpeg 提取帧（从本地文件，scale=1280, fps=1/interval）
  ├─ 评分（motion / CLIP / hybrid）
  ├─ [可选] LLaVA 对 Top N 峰值帧补充描述（--describe）
  ├─ 移动平均 + 峰值检测 → 高光时间段
  ├─ ffmpeg 精准裁切各片段（从本地文件）
  └─ [可选] ffmpeg concat 合并
```

## 输出结构

```
~/.openclaw/data/highlight-clip/<session-id>/
├── video.mp4         M3U8 下载合并的本地视频（M3U8 链接时生成）
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
| M3U8 并发下载（16 并发） | 1-2 分钟 |
| 提帧（149 帧，本地文件） | < 1 分钟 |
| motion 运动量计算 | < 30 秒 |
| CLIP 语义评分（149 帧） | < 1 分钟（CPU） |
| LLaVA 描述 5 帧（--describe） | 约 1 分钟 |
| 剪辑 5 个片段（本地文件） | < 1 分钟 |

---

## 评分模式对比

| 模式 | 命令 | 区分度 | 速度 | 硬件要求 | 适用场景 |
|------|------|--------|------|---------|---------|
| 纯运动量 | `--mode motion` | 好 | 最快 | CPU | 快速出片 |
| **CLIP 语义** | `--mode clip` | **更好** | 快 | CPU（~350MB 模型） | **推荐，理解画面内容** |
| 混合 | `--mode hybrid` | 最好 | 较快 | CPU | 兼顾运动量和语义 |

### 自定义 CLIP 评分语义

编辑 `scripts/clip_texts.json` 可自定义评分锚点文本：

```json
{
  "low_texts": [
    "a person talking to camera in an interview",
    "a person standing or sitting alone doing nothing",
    "a static wide shot of an empty room",
    "a title screen or credits text on black background",
    "a person slowly undressing alone"
  ],
  "high_texts": [
    "two people in intense close physical interaction",
    "vigorous dynamic body movement between people",
    "a close-up of expressive faces showing strong emotion",
    "fast rhythmic motion with multiple body parts",
    "an intense passionate climax moment"
  ]
}
```

- `low_texts`：低分锚点，描述平淡/过渡画面（采访、空镜、字幕等）
- `high_texts`：高分锚点，描述精彩/高光画面（激烈互动、动态运动、表情特写等）
- CLIP 计算每帧与两组文本的相似度差值，归一化为 0-100 分
- 根据具体业务调整文本可以提升评分精度
| + LLaVA 描述 | 加 `--describe` | 不影响 | +1min | 需要 Ollama | 需要了解片段内容 |

---

## 局限与解决方案

### 1. motion 模式无语义

**问题**：只看像素变化量，不理解内容，镜头切换等也会得高分。

**缓解**：用 `--mode clip` 替代，CLIP 理解画面语义；或加 `--describe` 让 LLaVA 描述峰值帧。

---

### 2. CLIP 模型选型

| CLIP 模型 | 参数 | 精度 | 速度 |
|-----------|------|------|------|
| `ViT-B-32`（默认） | 150M | ⭐⭐⭐ 够用 | 最快 |
| `ViT-L-14` | 400M | ⭐⭐⭐⭐ 更好 | 较慢 |
| `ViT-H-14` | 1B | ⭐⭐⭐⭐⭐ 最佳 | 慢 |

使用更大模型：
```bash
python3 scripts/highlight_clip.py <url> --mode clip \
  --clip-model ViT-L-14 --clip-pretrained laion2b_s32b_b82k
```

---

### 3. 成人内容只能自部署

**问题**：GPT-4o / Claude / Gemini 及国内主流 API（智谱、通义）均拒绝分析成人内容。

**解决**：CLIP（open_clip_torch）和 Ollama 均为本地运行，无内容审查。

---

## 公司部署推荐

### 推荐配置（CLIP 评分 + 可选 LLaVA 描述）

```bash
# 安装依赖（所有员工机器）
pip3 install open_clip_torch Pillow

# 基本使用：CLIP 评分，无需 GPU，无需 Ollama
python3 scripts/highlight_clip.py <m3u8_url> --mode clip --merge

# 需要文字描述时：配合 LLaVA（需要 Ollama）
python3 scripts/highlight_clip.py <m3u8_url> --mode clip --describe --merge
```

### LLaVA 描述服务（可选，需要 GPU）

如果需要 `--describe` 功能且本机跑不动 LLaVA，可租 GPU 服务器：

**服务器端：**
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llava:13b
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

**员工本机：**
```bash
export OLLAMA_HOST=http://公司服务器IP:11434
python3 scripts/highlight_clip.py <url> --mode clip --describe --model llava:13b --merge
```

| 平台 | 配置 | 可跑模型 | 费用 |
|------|------|---------|------|
| AutoDL | RTX 4090 (24G) | llava:13b | ~¥2/小时 |
| 矩池云 | A100 (40G) | llava:34b | ~¥10/小时 |
| 自购 | RTX 4090 整机 | llava:13b | ~¥2-3 万（一次性）|

> 业务量小先按小时租，跑量大再考虑包月或自购。

---

## 常见问题

**Q: 首次运行 CLIP 模式很慢？**
首次会下载 CLIP 模型（~350MB），之后会缓存到本地，后续启动秒级。

**Q: Ollama 连不上？（仅 --describe 需要）**
```bash
ollama serve        # 手动启动
ollama list         # 查看已下载模型
```

**Q: 提帧太慢？**
- 加大采样间隔：`-i 10`
- 减少帧数：`--max-frames 80`

**Q: 片段选得不准？**
- 试试 `--mode clip` 或 `--mode hybrid`
- 降低阈值：`-t 40`（更多候选）
- 缩小采样间隔：`-i 2`（更密集，更精准）
