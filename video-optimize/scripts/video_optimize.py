#!/usr/bin/env python3
"""
视频编码优化脚本
H.264 + .ts → H.265 + fMP4/CMAF

用法:
  python3 video_optimize.py download <m3u8_url> <name> [-c 16]
  python3 video_optimize.py transcode <name>
  python3 video_optimize.py direct <m3u8_url> <name> [-c 16]
  python3 video_optimize.py compare <name>
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urljoin

# -------------------- 配置 --------------------

@dataclass
class Config:
    preset: str = "medium"
    crf: int = 23
    hls_time: int = 6
    audio_bitrate: str = "128k"
    data_dir: Path = Path.home() / ".openclaw/data/video-optimize"

    def __post_init__(self):
        # 环境变量覆盖
        self.preset = os.environ.get("PRESET", self.preset)
        self.crf = int(os.environ.get("CRF", self.crf))
        self.hls_time = int(os.environ.get("HLS_TIME", self.hls_time))
        self.audio_bitrate = os.environ.get("AUDIO_BITRATE", self.audio_bitrate)
        self.data_dir = Path(os.environ.get("OUTPUT_BASE", self.data_dir))


# -------------------- 日志 --------------------

log = logging.getLogger("video-optimize")

def setup_logging():
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# -------------------- 工具函数 --------------------

def check_ffmpeg():
    """检查 ffmpeg 和 libx265 是否可用"""
    if not shutil.which("ffmpeg"):
        log.error("未安装 ffmpeg，请先安装: brew install ffmpeg")
        sys.exit(1)

    result = subprocess.run(
        ["ffmpeg", "-encoders"],
        capture_output=True, text=True
    )
    if "libx265" not in result.stdout:
        log.error("ffmpeg 不支持 libx265")
        sys.exit(1)


def run_ffmpeg(args: list[str], log_file: Path | None = None) -> bool:
    """运行 ffmpeg 命令，返回是否成功"""
    cmd = ["ffmpeg", "-y"] + args

    if log_file:
        with open(log_file, "w") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            log.error(f"ffmpeg 失败:\n{proc.stderr[-500:]}")

    return proc.returncode == 0


def dir_stats(path: Path, ext: str) -> tuple[str, int]:
    """返回目录大小和指定扩展名文件数"""
    result = subprocess.run(
        ["du", "-sh", str(path)],
        capture_output=True, text=True
    )
    size = result.stdout.split()[0] if result.stdout else "?"
    count = len(list(path.glob(f"*.{ext}")))
    return size, count


# -------------------- M3U8 并发下载 --------------------

def _fetch_text(url: str, timeout: int = 15) -> str:
    """下载文本内容"""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_m3u8(url: str) -> tuple[list[str], float]:
    """
    解析 M3U8 播放列表，返回 (分片 URL 列表, 总时长秒数)。
    自动处理 master playlist → 选最高带宽的 variant。
    """
    text = _fetch_text(url)
    lines = text.strip().splitlines()

    # master playlist：选最高带宽的 variant
    if any("#EXT-X-STREAM-INF" in l for l in lines):
        best_bw = -1
        best_uri = None
        for i, line in enumerate(lines):
            if "#EXT-X-STREAM-INF" in line:
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    if bw > best_bw:
                        best_bw = bw
                        best_uri = lines[i + 1].strip()
        if best_uri:
            variant_url = urljoin(url, best_uri)
            log.info(f"Master playlist → 选择最高画质（带宽: {best_bw}）")
            return parse_m3u8(variant_url)

    # media playlist：解析分片
    segments = []
    total_duration = 0.0
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            m = re.search(r"#EXTINF:([\d.]+)", line)
            if m:
                total_duration += float(m.group(1))
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j].startswith("#"):
                    seg_url = urljoin(url, lines[j].strip())
                    segments.append(seg_url)
                    break

    return segments, total_duration


def _download_segment(args_tuple: tuple) -> tuple[int, bool, str]:
    """下载单个分片，失败重试 3 次"""
    idx, seg_url, output_path, retries = args_tuple
    for attempt in range(retries + 1):
        try:
            req = Request(seg_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
            Path(output_path).write_bytes(data)
            return idx, True, ""
        except Exception as e:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return idx, False, str(e)


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    filled = round(done / total * width) if total else 0
    return "█" * filled + "░" * (width - filled)


def download_m3u8_fast(url: str, output_path: Path, concurrency: int = 16) -> bool:
    """
    并发下载 M3U8 所有分片，合并为本地 .ts 文件。
    返回是否成功。
    """
    log.info(f"解析 M3U8 播放列表...")
    try:
        segments, duration = parse_m3u8(url)
    except Exception as e:
        log.error(f"M3U8 解析失败: {e}")
        return False

    if not segments:
        log.error("未解析到分片")
        return False

    total = len(segments)
    dur_str = time.strftime("%H:%M:%S", time.gmtime(duration)) if duration else "?"
    log.info(f"共 {total} 个分片（约 {dur_str}），{concurrency} 并发下载...")

    # 临时目录
    seg_dir = output_path.parent / "_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    tasks = []
    for i, seg_url in enumerate(segments):
        seg_path = str(seg_dir / f"seg_{i:05d}.ts")
        tasks.append((i, seg_url, seg_path, 3))

    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_download_segment, t): t for t in tasks}
        for fut in as_completed(futures):
            idx, ok, err = fut.result()
            completed += 1
            if not ok:
                failed += 1
            print(f"\r   [{_progress_bar(completed, total)}] {completed}/{total}"
                  f'{"  (" + str(failed) + " 失败)" if failed else ""}',
                  end="", flush=True)
    print()

    if failed > total * 0.1:
        log.error(f"失败率过高（{failed}/{total}），放弃")
        shutil.rmtree(seg_dir, ignore_errors=True)
        return False

    # 用 ffmpeg concat demuxer 正确合并分片
    log.info("合并分片...")
    seg_files = sorted(seg_dir.glob("seg_*.ts"))
    concat_list = seg_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{sf}'" for sf in seg_files))

    concat_cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy", str(output_path),
    ]
    proc = subprocess.run(concat_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error(f"ffmpeg 合并失败，回退到字节拼接")
        with open(output_path, "wb") as out:
            for sf in seg_files:
                out.write(sf.read_bytes())

    shutil.rmtree(seg_dir, ignore_errors=True)

    size_mb = output_path.stat().st_size / 1024 / 1024
    log.info(f"下载完成: {output_path} ({size_mb:.1f} MB)")
    return True


# -------------------- 命令实现 --------------------

def cmd_download(url: str, name: str, cfg: Config, concurrency: int = 16) -> bool:
    """下载远程 m3u8 并解密为本地 H.264 .ts"""
    out_dir = cfg.data_dir / f"{name}_original"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"下载并解密: {url}")
    log.info(f"输出: {out_dir}/")

    # 并发下载到本地
    local_ts = out_dir / "source.ts"
    if not download_m3u8_fast(url, local_ts, concurrency):
        log.error(f"并发下载失败，回退到 ffmpeg: {name}")
        local_ts = None

    # 用本地文件（或原始 URL）重新分片为 HLS
    input_src = str(local_ts) if local_ts and local_ts.exists() else url
    ok = run_ffmpeg([
        "-i", input_src,
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "5",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", str(out_dir / "seg-%04d.ts"),
        str(out_dir / "playlist.m3u8"),
    ])

    # 清理临时源文件
    if local_ts and local_ts.exists():
        local_ts.unlink(missing_ok=True)

    if ok:
        size, count = dir_stats(out_dir, "ts")
        log.info(f"下载完成: {count} 个片段, {size}")
    else:
        log.error(f"下载失败: {name}")

    return ok


def cmd_transcode(name: str, cfg: Config) -> bool:
    """将本地 H.264 .ts 转码为 H.265 fMP4/CMAF"""
    in_dir = cfg.data_dir / f"{name}_original"
    out_dir = cfg.data_dir / f"{name}_h265_fmp4"
    playlist = in_dir / "playlist.m3u8"

    if not playlist.exists():
        log.error(f"找不到 {playlist}，请先运行 download")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)

    in_size, _ = dir_stats(in_dir, "ts")
    log.info(f"转码: H.264 .ts → H.265 fMP4/CMAF")
    log.info(f"输入: {in_dir}/ ({in_size})")
    log.info(f"参数: preset={cfg.preset}, crf={cfg.crf}, 分片={cfg.hls_time}秒")

    ok = run_ffmpeg([
        "-i", str(playlist),
        "-c:v", "libx265",
        "-preset", cfg.preset,
        "-crf", str(cfg.crf),
        "-tag:v", "hvc1",
        "-c:a", "aac",
        "-b:a", cfg.audio_bitrate,
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_time", str(cfg.hls_time),
        "-hls_playlist_type", "vod",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg-%03d.m4s"),
        str(out_dir / "playlist.m3u8"),
    ])

    if ok:
        size, count = dir_stats(out_dir, "m4s")
        log.info(f"转码完成: {count} 个片段, {size}")
        cmd_compare(name, cfg)
    else:
        log.error(f"转码失败: {name}")

    return ok


def cmd_direct(url: str, name: str, cfg: Config, concurrency: int = 16) -> bool:
    """一步到位：直接从远程转码为 H.265 fMP4"""
    out_dir = cfg.data_dir / f"{name}_h265_fmp4"
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"直接转码: {url}")
    log.info(f"输出: {out_dir}/")
    log.info(f"参数: H.265, preset={cfg.preset}, crf={cfg.crf}, fMP4/CMAF")

    # 并发下载到本地
    local_ts = out_dir / "source.ts"
    if not download_m3u8_fast(url, local_ts, concurrency):
        log.error(f"并发下载失败，回退到 ffmpeg: {name}")
        local_ts = None

    input_src = str(local_ts) if local_ts and local_ts.exists() else url
    ok = run_ffmpeg([
        "-i", input_src,
        "-c:v", "libx265",
        "-preset", cfg.preset,
        "-crf", str(cfg.crf),
        "-tag:v", "hvc1",
        "-c:a", "aac",
        "-b:a", cfg.audio_bitrate,
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_time", str(cfg.hls_time),
        "-hls_playlist_type", "vod",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg-%03d.m4s"),
        str(out_dir / "playlist.m3u8"),
    ])

    # 清理临时源文件
    if local_ts and local_ts.exists():
        local_ts.unlink(missing_ok=True)

    if ok:
        size, count = dir_stats(out_dir, "m4s")
        log.info(f"转码完成: {count} 个片段, {size}")
    else:
        log.error(f"转码失败: {name}")

    return ok


def cmd_compare(name: str, cfg: Config):
    """对比原始和优化后的体积"""
    orig_dir = cfg.data_dir / f"{name}_original"
    opt_dir = cfg.data_dir / f"{name}_h265_fmp4"

    print()
    print("==========================================")
    print(f"对比: {name}")
    print("==========================================")

    if orig_dir.is_dir():
        size, count = dir_stats(orig_dir, "ts")
        print(f"  原始 (H.264 + .ts):      {size}   {count} 个片段")
    else:
        print("  原始: 未下载（跳过）")

    if opt_dir.is_dir():
        size, count = dir_stats(opt_dir, "m4s")
        print(f"  优化 (H.265 + fMP4):     {size}   {count} 个片段")
    else:
        print("  优化: 未转码（跳过）")

    print("==========================================")
    print()


# -------------------- CLI --------------------

def main():
    setup_logging()
    cfg = Config()

    parser = argparse.ArgumentParser(
        description="视频编码优化 - H.264+.ts → H.265+fMP4/CMAF"
    )
    sub = parser.add_subparsers(dest="command")

    # download
    p = sub.add_parser("download", help="下载远程 m3u8 并解密")
    p.add_argument("url", help="m3u8 地址")
    p.add_argument("name", help="输出名称")
    p.add_argument("-c", "--concurrency", type=int, default=16, help="分片并发下载数（默认 16）")

    # transcode
    p = sub.add_parser("transcode", help="转码为 H.265 fMP4/CMAF")
    p.add_argument("name", help="视频名称（需先 download）")

    # direct
    p = sub.add_parser("direct", help="一步到位（下载+转码）")
    p.add_argument("url", help="m3u8 地址")
    p.add_argument("name", help="输出名称")
    p.add_argument("-c", "--concurrency", type=int, default=16, help="分片并发下载数（默认 16）")

    # compare
    p = sub.add_parser("compare", help="对比体积")
    p.add_argument("name", help="视频名称")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    check_ffmpeg()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "download":
        cmd_download(args.url, args.name, cfg, args.concurrency)
    elif args.command == "transcode":
        cmd_transcode(args.name, cfg)
    elif args.command == "direct":
        cmd_direct(args.url, args.name, cfg, args.concurrency)
    elif args.command == "compare":
        cmd_compare(args.name, cfg)


if __name__ == "__main__":
    main()
