---
name: video-optimize
description: 将公司存量或新增的 m3u8 视频从 H.264+.ts 优化为 H.265+fMP4/CMAF 格式，降低存储和带宽成本，提升播放体验。触发场景：用户说"视频优化"、"转码"、"H.265"、"fMP4"、"/video-optimize" 时使用。
allowed-tools: Bash(python3:*) Bash(ffmpeg:*) Bash(ffprobe:*) Bash(du:*) Bash(ls:*)
---

# 视频编码优化（H.265 + fMP4/CMAF）

将 m3u8 视频从传统 H.264 + .ts 格式优化为现代 H.265 + fMP4/CMAF 格式。

## 依赖

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# 验证 H.265 支持
ffmpeg -encoders 2>/dev/null | grep libx265
```

无额外 Python 依赖，仅使用标准库。

## 快速开始

```bash
# 单个视频：下载解密 + 转码（两步，保留原始用于对比）
python3 scripts/video_optimize.py download "https://example.com/video.m3u8" "VIDEO_001"
python3 scripts/video_optimize.py transcode "VIDEO_001"

# 单个视频：一步到位
python3 scripts/video_optimize.py direct "https://example.com/video.m3u8" "VIDEO_001"

# 指定并发下载数（默认 16）
python3 scripts/video_optimize.py direct "https://example.com/video.m3u8" "VIDEO_001" -c 32

# 对比体积
python3 scripts/video_optimize.py compare "VIDEO_001"
```

## 完整参数

```
python3 scripts/video_optimize.py <command> [参数]

命令:
  download <url> <name> [-c N]       下载远程 m3u8 并解密为本地 H.264 .ts
  transcode <name>                   将本地 .ts 转码为 H.265 fMP4/CMAF
  direct <url> <name> [-c N]         一步到位（下载+转码）
  compare <name>                     对比原始和优化后的体积

通用参数:
  -c, --concurrency <数>  M3U8 分片并发下载数（默认 16）

环境变量（可选）:
  PRESET=medium          编码速度 (ultrafast/fast/medium/slow/veryslow)
  CRF=23                 质量因子 (0-51, 越小画质越好)
  HLS_TIME=6             分片时长（秒）
  AUDIO_BITRATE=128k     音频码率
  OUTPUT_BASE=<路径>     输出根目录
```

## 工作流程

```
download 命令:
  远程 m3u8 (可能加密)
    ↓ 并发下载分片（-c 控制并发数）→ 本地 source.ts
    ↓ FFmpeg -c copy (重新分片为 HLS)
  <name>_original/          H.264 + .ts (解密后)

transcode 命令:
  <name>_original/
    ↓ FFmpeg libx265 + fMP4
  <name>_h265_fmp4/         H.265 + fMP4/CMAF

direct 命令:
  远程 m3u8 → 并发下载（-c 控制并发数）→ 本地 source.ts → 转码 → <name>_h265_fmp4/
```

## 输出结构

```
~/.openclaw/data/video-optimize/
├── VIDEO_001_original/       原始 H.264 + .ts
├── VIDEO_001_h265_fmp4/      优化后 H.265 + fMP4
└── VIDEO_002_h265_fmp4/
```

## 实测对比参考

```
测试视频: 123 分钟, 1920×1080

┌─────────────────────────┬──────────┬──────────┐
│         格式             │   体积    │  分片数   │
├─────────────────────────┼──────────┼──────────┤
│ 现有 H.264 + .ts        │  492 MB  │  374 个  │
│ 优化 H.265 + fMP4/CMAF  │  483 MB  │  327 个  │
└─────────────────────────┴──────────┴──────────┘

分片减少 12.6%，高码率视频可达 30-50% 体积缩减
```

## 常见问题

**Q: ffmpeg 不支持 libx265？**
重新安装带 H.265 支持的版本：`brew install ffmpeg`

**Q: 转码很慢？**
- `PRESET=fast` 或 `PRESET=ultrafast` 加速
- `CRF=28` 画质略降但速度快很多

**Q: H.265 浏览器兼容性？**
hls.js 在主流浏览器均支持，Safari 原生支持。
