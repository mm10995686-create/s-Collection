#!/usr/bin/env python3
"""
highlight_clip.py — 高光时刻自动剪辑
通过帧间运动量检测 + 可选 LLaVA 语义评分，识别并剪辑出最精彩的片段

评分策略（--mode 控制）：
  motion  : 帧间像素差（默认，最快，无内容限制）
  llava   : LLaVA 语义评分（需要 Ollama，慢但理解内容）
  hybrid  : 运动量 * 0.6 + LLaVA * 0.4（兼顾两者）

数据目录：~/.openclaw/data/highlight-clip/<session-id>/
"""

import os
import sys
import json
import argparse
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import re

from PIL import Image, ImageChops

# ──────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────

BASE_DIR = Path.home() / '.openclaw' / 'data' / 'highlight-clip'
OLLAMA_HOST = os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434')

# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────

def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'

def progress_bar(done: int, total: int, width: int = 20) -> str:
    filled = round(done / total * width) if total else 0
    return '█' * filled + '░' * (width - filled)

def now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def run_cmd(cmd: List[str], timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)

# ──────────────────────────────────────────────────────────────
# 依赖检查
# ──────────────────────────────────────────────────────────────

def check_dependencies(args: argparse.Namespace) -> None:
    for bin_ in ['ffmpeg', 'ffprobe']:
        if not shutil.which(bin_):
            print(f'❌ 未找到 {bin_}，请先安装：brew install ffmpeg')
            sys.exit(1)

    if args.mode in ('llava', 'hybrid'):
        try:
            import ollama as _ollama
            client = _ollama.Client(host=OLLAMA_HOST)
            models = client.list()
            names = [m.model for m in models.models]
            if not any(n.startswith(args.model) for n in names):
                print(f'❌ 模型 {args.model} 未下载，请运行：ollama pull {args.model}')
                print(f'   已有模型：{", ".join(names) or "（无）"}')
                sys.exit(1)
            print(f'✅ 依赖检查通过（模型: {args.model}，模式: {args.mode}）')
        except Exception as e:
            print(f'❌ 无法连接 Ollama（{OLLAMA_HOST}）：{e}')
            sys.exit(1)
    else:
        print(f'✅ 依赖检查通过（模式: motion，纯本地运算）')

# ──────────────────────────────────────────────────────────────
# 1. 获取视频时长
# ──────────────────────────────────────────────────────────────

def get_video_duration(url: str) -> float:
    print('📐 获取视频信息...')
    try:
        result = run_cmd([
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', url
        ], timeout=30)
        dur = float(json.loads(result.stdout).get('format', {}).get('duration', 0))
        if dur > 0:
            print(f'   时长：{fmt_time(dur)}（{round(dur)} 秒）')
            return dur
    except Exception:
        pass
    print('   ⚠️  无法获取时长，最多采集 max-frames 帧')
    return 0.0

# ──────────────────────────────────────────────────────────────
# 2. 提取视频帧
# ──────────────────────────────────────────────────────────────

def extract_frames(url: str, frames_dir: Path, interval: float, max_frames: int) -> List[Dict]:
    # 清空旧帧，避免多次运行叠加导致时间戳错乱
    if frames_dir.exists():
        for f in frames_dir.glob('frame_*.jpg'):
            f.unlink()
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(frames_dir / 'frame_%04d.jpg')
    fps = 1.0 / interval

    print(f'\n🎬 提取视频帧（每 {interval:.0f}s 取 1 帧，最多 {max_frames} 帧）...')

    proc = subprocess.Popen(
        ['ffmpeg', '-i', url,
         '-vf', f'fps={fps:.6f},scale=1280:-2',
         '-q:v', '3', '-frames:v', str(max_frames), pattern, '-y'],
        stderr=subprocess.PIPE, text=True
    )
    last_time = ''
    for line in proc.stderr:
        m = re.search(r'time=(\d+:\d+:\d+)', line)
        if m and m.group(1) != last_time:
            last_time = m.group(1)
            print(f'\r   进度: {last_time}', end='', flush=True)
    proc.wait()
    print()

    files = sorted(frames_dir.glob('frame_*.jpg'))
    if not files:
        print('❌ 未能提取任何帧，请检查视频地址')
        sys.exit(1)

    frames = [{'index': i, 'timestamp': i * interval, 'path': str(f)} for i, f in enumerate(files)]
    print(f'   ✅ 共提取 {len(frames)} 帧（覆盖约 {fmt_time(frames[-1]["timestamp"] + interval)}）')
    return frames

# ──────────────────────────────────────────────────────────────
# 3a. 运动量评分（帧间像素差）
# ──────────────────────────────────────────────────────────────

def compute_motion_scores(frames: List[Dict]) -> List[Dict]:
    """
    计算每帧与前后帧的像素差，差值越大说明画面运动越激烈。
    将原始差值归一化到 0-100 分。
    """
    print(f'\n📊 计算运动量（帧间像素差）...')
    SIZE = (160, 90)  # 缩小后计算，速度快

    # 读取所有帧的灰度缩略图
    thumbs = []
    for f in frames:
        img = Image.open(f['path']).convert('L').resize(SIZE)
        thumbs.append(list(img.getdata()))

    # 计算每帧与前后帧的平均差值
    raw_scores = []
    n = len(thumbs)
    for i in range(n):
        diffs = []
        if i > 0:
            d = sum(abs(a - b) for a, b in zip(thumbs[i], thumbs[i-1])) / len(thumbs[i])
            diffs.append(d)
        if i < n - 1:
            d = sum(abs(a - b) for a, b in zip(thumbs[i], thumbs[i+1])) / len(thumbs[i])
            diffs.append(d)
        raw_scores.append(sum(diffs) / len(diffs) if diffs else 0)

    # 归一化到 0-100
    min_s, max_s = min(raw_scores), max(raw_scores)
    rng = max_s - min_s if max_s > min_s else 1
    results = []
    for i, (frame, raw) in enumerate(zip(frames, raw_scores)):
        score = round((raw - min_s) / rng * 100)
        results.append({**frame, 'score': score, 'description': f'运动量{score}', 'raw_motion': raw})
        print(f'\r   [{progress_bar(i+1, n)}] {i+1}/{n}', end='', flush=True)

    print(f'\n   ✅ 运动量计算完成')
    return results

# ──────────────────────────────────────────────────────────────
# 3b. LLaVA 语义评分
# ──────────────────────────────────────────────────────────────

LLAVA_DESC_PROMPT = "用一句话（15字以内）描述这张视频截图的画面内容，只说看到的画面，不要评价好坏。只返回描述文字，不要其他内容。"

LLAVA_PROMPT = """这是一段视频的截图。请评估这一帧画面的"精彩程度"（0-100分）。
评分标准（严格区分，大多数普通画面应在 30-50 分）：
  0-20:  画面静止，单人无动作，无特别之处
  20-40: 轻微动作或变化
  40-60: 中等强度，有明显动作或情绪
  60-80: 高强度动作，激烈运动或强烈情绪
  80-100: 极度激烈的高潮时刻

只返回 JSON，不要其他内容：{"score":45,"desc":"一句话描述"}"""

def analyze_frame_llava(frame: Dict, model: str, retries: int = 2) -> Dict:
    import ollama as _ollama
    client = _ollama.Client(host=OLLAMA_HOST)

    # 缩小图片减少处理时间
    img = Image.open(frame['path'])
    w, h = img.size
    if w > 768:
        img = img.resize((768, int(h * 768 / w)), Image.LANCZOS)
    tmp = frame['path'].replace('.jpg', '_s.jpg')
    img.save(tmp, 'JPEG', quality=80)

    for attempt in range(retries + 1):
        try:
            resp = client.chat(
                model=model,
                messages=[{'role': 'user', 'content': LLAVA_PROMPT, 'images': [tmp]}],
                options={'temperature': 0.1},
            )
            text = resp.message.content.strip()
            m = re.search(r'\{[\s\S]*?\}', text)
            if m:
                p = json.loads(m.group())
                score = max(0, min(100, int(p.get('score', 0))))
                desc  = str(p.get('desc', '')).strip()[:20]
                return {**frame, 'score': score, 'description': desc}
            # 没有 JSON，提取第一个数字
            nums = re.findall(r'\b(\d{1,3})\b', text)
            return {**frame, 'score': max(0, min(100, int(nums[0]))) if nums else 0, 'description': text[:20]}
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {**frame, 'score': 0, 'description': '分析失败', 'error': str(e)}
    return {**frame, 'score': 0, 'description': ''}

def analyze_frames_llava(frames: List[Dict], model: str, concurrency: int) -> List[Dict]:
    results = [None] * len(frames)
    completed = 0
    total = len(frames)
    print(f'\n🤖 LLaVA 语义评分 {total} 帧（并发: {concurrency}）...')

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(analyze_frame_llava, f, model): f for f in frames}
        for fut in as_completed(futures):
            r = fut.result()
            results[r['index']] = r
            completed += 1
            print(f'\r   [{progress_bar(completed, total)}] {completed}/{total}  '
                  f'{fmt_time(r["timestamp"])} → {r.get("score",0):3d}分  {r.get("description","")[:8]}',
                  end='', flush=True)
    print()
    failed = sum(1 for r in results if r and r.get('error'))
    if failed:
        print(f'   ⚠️  {failed} 帧失败')
    print('   ✅ LLaVA 评分完成')
    return results

# ──────────────────────────────────────────────────────────────
# 3c. 仅对高光峰值帧补充 LLaVA 描述（motion 模式专用）
# ──────────────────────────────────────────────────────────────

def describe_peak_frames(highlights: List[Dict], analyses: List[Dict], model: str) -> List[Dict]:
    """找到每个高光片段的峰值帧，调用 LLaVA 补充描述，不影响评分。"""
    import ollama as _ollama
    client = _ollama.Client(host=OLLAMA_HOST)

    # 建立 timestamp → analysis 的快速索引
    ts_map = {a['timestamp']: a for a in analyses}

    print(f'\n🔎 LLaVA 补充描述（{len(highlights)} 个高光帧）...')
    for h in highlights:
        # 找峰值帧对应的 analysis
        peak_ts = None
        best_score = -1
        for a in analyses:
            if h['startTime'] <= a['timestamp'] <= h['endTime'] and a['score'] > best_score:
                best_score = a['score']
                peak_ts = a['timestamp']

        if peak_ts is None:
            continue

        peak_frame = ts_map[peak_ts]
        img_path = peak_frame['path']

        # 缩图
        img = Image.open(img_path)
        w, h_px = img.size
        if w > 768:
            img = img.resize((768, int(h_px * 768 / w)), Image.LANCZOS)
        tmp = img_path.replace('.jpg', '_d.jpg')
        img.save(tmp, 'JPEG', quality=80)

        try:
            resp = client.chat(
                model=model,
                messages=[{'role': 'user', 'content': LLAVA_DESC_PROMPT, 'images': [tmp]}],
                options={'temperature': 0.1},
            )
            desc = resp.message.content.strip().strip('"').strip('。')[:25]
            h['description'] = desc
            print(f'   [{h["rank"]}] {fmt_time(h["startTime"])} → {desc}')
        except Exception as e:
            print(f'   [{h["rank"]}] 描述失败：{e}')

    return highlights

# ──────────────────────────────────────────────────────────────
# 3. 评分入口（根据 mode 选择策略）
# ──────────────────────────────────────────────────────────────

def score_frames(frames: List[Dict], args: argparse.Namespace) -> List[Dict]:
    if args.mode == 'motion':
        return compute_motion_scores(frames)

    elif args.mode == 'llava':
        return analyze_frames_llava(frames, args.model, args.concurrency)

    else:  # hybrid
        print('\n🔀 混合模式：运动量 × 0.6 + LLaVA × 0.4')
        motion_results = compute_motion_scores(frames)
        llava_results  = analyze_frames_llava(frames, args.model, args.concurrency)

        merged = []
        for m, l in zip(motion_results, llava_results):
            score = round(m['score'] * 0.6 + l['score'] * 0.4)
            merged.append({**m, 'score': score,
                           'motion_score': m['score'],
                           'llava_score':  l['score'],
                           'description':  l.get('description', '')})
        return merged

# ──────────────────────────────────────────────────────────────
# 4. 高光片段检测
# ──────────────────────────────────────────────────────────────

def detect_highlights(analyses: List[Dict], threshold: int,
                      clip_duration: float, top_count: int, interval: float) -> List[Dict]:
    if not analyses:
        return []

    scores = [a['score'] for a in analyses]
    if max(scores) == 0:
        print('   ❌ 所有帧评分为 0，无法识别高光')
        return []

    # 移动平均平滑（窗口 3 帧）
    smoothed = []
    for i in range(len(scores)):
        win = scores[max(0, i-1):min(len(scores), i+2)]
        smoothed.append(sum(win) / len(win))

    # 自适应阈值
    eff_threshold = threshold
    if not any(s >= threshold for s in smoothed):
        sorted_s = sorted(smoothed, reverse=True)
        eff_threshold = int(sorted_s[int(len(sorted_s) * 0.2)])
        print(f'   ⚠️  无帧超过 {threshold} 分，自动降至 {eff_threshold} 分')

    # 聚合高分帧为片段
    half = clip_duration / 2
    segments = []
    for i, a in enumerate(analyses):
        if smoothed[i] < eff_threshold:
            continue
        seg_start = max(0.0, a['timestamp'] - half)
        seg_end   = a['timestamp'] + half
        merged = False
        for seg in segments:
            if seg_start <= seg['end'] + interval and seg_end >= seg['start'] - interval:
                seg['start'] = min(seg['start'], seg_start)
                seg['end']   = max(seg['end'],   seg_end)
                seg['frames'].append(a)
                merged = True
                break
        if not merged:
            segments.append({'start': seg_start, 'end': seg_end, 'frames': [a]})

    if not segments:
        return []

    scored = []
    for seg in segments:
        seg_scores = [f['score'] for f in seg['frames']]
        peak = max(seg_scores)
        avg  = round(sum(seg_scores) / len(seg_scores))
        peak_frame = next(f for f in seg['frames'] if f['score'] == peak)
        scored.append({
            'startTime':   round(seg['start'], 1),
            'endTime':     round(min(seg['end'], seg['start'] + clip_duration * 1.5), 1),
            'peakScore':   peak,
            'avgScore':    avg,
            'description': peak_frame.get('description', ''),
        })
        scored[-1]['duration'] = round(scored[-1]['endTime'] - scored[-1]['startTime'], 1)

    scored.sort(key=lambda x: x['avgScore'], reverse=True)
    selected = scored[:top_count]
    selected.sort(key=lambda x: x['startTime'])
    for i, s in enumerate(selected):
        s['rank'] = i + 1
    return selected

# ──────────────────────────────────────────────────────────────
# 5. 剪辑片段
# ──────────────────────────────────────────────────────────────

def extract_clip(url: str, seg: Dict, output_path: Path, verbose: bool) -> Optional[str]:
    duration = seg['endTime'] - seg['startTime']
    cmd = [
        'ffmpeg', '-ss', str(seg['startTime']), '-i', url,
        '-t', str(round(duration, 2)),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        str(output_path), '-y'
    ]
    if verbose:
        print(f'\n   cmd: {" ".join(cmd)}')
    try:
        # 超时 = 片段时长 × 10（下载 + 编码），最少 120s
        timeout = max(120, int(duration * 10))
        run_cmd(cmd, timeout=timeout)
        return str(output_path)
    except Exception as e:
        print(f'\n   ❌ 剪辑失败：{e}')
        return None

def extract_clips(url: str, highlights: List[Dict], session_dir: Path, verbose: bool) -> List[Dict]:
    print(f'\n✂️  剪辑 {len(highlights)} 个高光片段...')
    clips = []
    for seg in highlights:
        rank = seg['rank']
        out  = session_dir / f'clip_{rank:02d}.mp4'
        label = (f'[{rank}/{len(highlights)}] '
                 f'{fmt_time(seg["startTime"])}–{fmt_time(seg["endTime"])} '
                 f'({round(seg["duration"])}s, 均分={seg["avgScore"]})  '
                 f'"{seg["description"]}"')
        print(f'   {label}', end='', flush=True)
        result = extract_clip(url, seg, out, verbose)
        if result:
            print('  ✅')
            clips.append({**seg, 'outputPath': result})
        else:
            print('  ❌')
    return clips

# ──────────────────────────────────────────────────────────────
# 6. 合并片段
# ──────────────────────────────────────────────────────────────

def merge_clips(clips: List[Dict], session_dir: Path) -> Optional[str]:
    if len(clips) < 2:
        return None
    list_path   = session_dir / '_filelist.txt'
    merged_path = session_dir / 'highlights.mp4'
    ordered = sorted(clips, key=lambda c: c['startTime'])
    list_path.write_text('\n'.join(f"file '{c['outputPath']}'" for c in ordered))
    print(f'\n🔗 合并 {len(clips)} 个片段...')
    try:
        run_cmd(['ffmpeg', '-f', 'concat', '-safe', '0',
                 '-i', str(list_path), '-c', 'copy', str(merged_path), '-y'], timeout=300)
        list_path.unlink(missing_ok=True)
        print(f'   ✅ {merged_path}')
        return str(merged_path)
    except Exception as e:
        print(f'   ❌ 合并失败：{e}')
        return None

# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='高光时刻自动剪辑')
    p.add_argument('url',                                       help='M3U8 视频地址')
    p.add_argument('-o', '--output-dir',                        help='输出目录')
    p.add_argument('--mode',          default='motion',
                   choices=['motion', 'llava', 'hybrid'],       help='评分模式（默认 motion）')
    p.add_argument('-i', '--interval',  type=float, default=3,  help='采样间隔秒（默认 3）')
    p.add_argument('--max-frames',      type=int,   default=150,help='最多采样帧数（默认 150）')
    p.add_argument('-d', '--clip-duration', type=float, default=20, help='每片段时长（默认 20）')
    p.add_argument('-n', '--count',     type=int,   default=5,  help='输出片段数（默认 5）')
    p.add_argument('-t', '--threshold', type=int,   default=60, help='高光阈值 0-100（默认 60）')
    p.add_argument('-m', '--merge',     action='store_true',    help='合并为 highlights.mp4')
    p.add_argument('-c', '--concurrency', type=int, default=2,  help='LLaVA 并发数（默认 2）')
    p.add_argument('--model',          default='llava',         help='Ollama 模型（默认 llava）')
    p.add_argument('--describe',       action='store_true',     help='motion 模式下用 LLaVA 对高光帧补充描述')
    p.add_argument('--no-clip',        action='store_true',     help='仅分析，不剪辑')
    p.add_argument('-v', '--verbose',  action='store_true',     help='详细日志')
    return p.parse_args()

# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    session_id  = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    session_dir = Path(args.output_dir) if args.output_dir else BASE_DIR / session_id
    frames_dir  = session_dir / 'frames'
    session_dir.mkdir(parents=True, exist_ok=True)

    print('╔═══════════════════════════════════════════╗')
    print('║     高光时刻剪辑  ·  Highlight Clip       ║')
    print('╚═══════════════════════════════════════════╝')
    print(f'📁 输出目录:  {session_dir}')
    print(f'🎯 配置:     间隔={args.interval}s | 阈值={args.threshold} | 片段数={args.count} | 时长={args.clip_duration}s')
    print(f'🔍 评分模式: {args.mode}')
    print(f'⏰ 开始时间: {now_str()}')
    print()

    check_dependencies(args)

    # Step 1: 时长
    duration = get_video_duration(args.url)
    interval = args.interval
    if duration > 0:
        min_interval = duration / args.max_frames
        if min_interval > interval:
            interval = round(min_interval)
            print(f'   ℹ️  视频较长，采样间隔自动调整为 {interval}s')

    # Step 2: 提帧
    frames = extract_frames(args.url, frames_dir, interval, args.max_frames)

    # Step 3: 评分
    analyses = score_frames(frames, args)

    analysis_path = session_dir / 'analysis.json'
    analysis_path.write_text(json.dumps(analyses, ensure_ascii=False, indent=2))
    print(f'\n💾 分析结果: {analysis_path}')

    # 打印评分分布
    scores = [a['score'] for a in analyses]
    buckets = {}
    for s in scores:
        k = (s // 10) * 10
        buckets[k] = buckets.get(k, 0) + 1
    print('   评分分布: ' + '  '.join(f'{k}-{k+9}:{v}帧' for k, v in sorted(buckets.items()) if v))

    # Step 4: 识别高光
    print('\n🔍 识别高光时刻...')
    highlights = detect_highlights(analyses, args.threshold, args.clip_duration, args.count, interval)

    if not highlights:
        print('❌ 未找到高光片段，请尝试降低 --threshold')
        sys.exit(1)

    # motion 模式下可选用 LLaVA 补充描述
    if args.mode == 'motion' and args.describe:
        highlights = describe_peak_frames(highlights, analyses, args.model)

    print(f'   发现 {len(highlights)} 个高光片段：')
    for h in highlights:
        print(f'   [{h["rank"]}] {fmt_time(h["startTime"])}–{fmt_time(h["endTime"])}'
              f'  峰值={h["peakScore"]}  均分={h["avgScore"]}  "{h["description"]}"')

    (session_dir / 'highlights.json').write_text(json.dumps(highlights, ensure_ascii=False, indent=2))

    # Step 5: 剪辑
    if args.no_clip:
        print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
        print('✅ 分析完成（--no-clip 模式）')
        return

    clips = extract_clips(args.url, highlights, session_dir, args.verbose)

    # Step 6: 合并
    if args.merge and len(clips) > 1:
        merge_clips(clips, session_dir)

    print('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    print(f'✅ 完成！共剪辑 {len(clips)} 个高光片段')
    print(f'📂 输出目录: {session_dir}')
    for c in clips:
        name = Path(c['outputPath']).name
        print(f'  [{c["rank"]}] {name}  {fmt_time(c["startTime"])}–{fmt_time(c["endTime"])}'
              f'  均分={c["avgScore"]}  "{c["description"]}"')
    if args.merge and len(clips) > 1:
        print(f'  🎬 合并版: {session_dir}/highlights.mp4')
    print(f'\n⏰ 结束时间: {now_str()}')


if __name__ == '__main__':
    main()
