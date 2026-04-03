#!/usr/bin/env python3
"""
域名封锁检测 - 通过 itdog.cn 多节点 HTTP 检测
使用 Selenium headless Chrome 模拟真实用户操作，完整走页面流程
成功率 < 70% 视为疑似被封

子命令:
  add <域名...>       添加域名到监控列表
  remove <域名...>    从监控列表删除域名
  list                查看当前监控列表
  run                 立即检测监控列表中所有域名（可由外部调度器定时触发）
                        --batch-size/-B N   分批检测，每批 N 个（0=不分批）
                        --batch-delay S     批次间隔秒数（默认 30s）
  sync <url>          从远程 JSON 拉取域名写入本地文件（供定时调度器调用）
  <域名...>           一次性检测指定域名（不影响监控列表）

域名无协议前缀时自动补 https://（如 e536.eohdbxs.cc → https://e536.eohdbxs.cc）
"""

import sys
import json
import argparse
import time
import os
import tempfile
import shutil
import random
import threading
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple, Any

DEFAULT_THRESHOLD   = 0.7
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY     = 5
# 远程 sync：拉取失败时重试（含首次共 4 次 = 失败后最多再试 3 次）
SYNC_FETCH_ATTEMPTS = 4
SYNC_RETRY_DELAY_S  = 5
WATCHLIST_PATH      = os.path.expanduser("~/.openclaw/data/check-domain/watchlist.json")
LOG_PATH            = os.path.expanduser("~/.openclaw/data/check-domain/check.log")
SYNCED_MAP_PATH     = os.path.expanduser("~/.openclaw/data/check-domain/synced_map.json")
SYNC_META_PATH      = os.path.expanduser("~/.openclaw/data/check-domain/sync_meta.json")

# Chrome 启动错峰：两次启动之间最少间隔（秒），加随机抖动
LAUNCH_GAP_MIN    = 3.0
LAUNCH_GAP_JITTER = 2.0

_print_lock       = threading.Lock()
_launch_lock      = threading.Lock()
_last_launch_time = 0.0


def _safe_print(msg: str, prefix: str = ""):
    """线程安全的 print，并发模式下加域名前缀"""
    with _print_lock:
        if prefix:
            print(f"[{prefix}] {msg}")
        else:
            print(msg)


# ──────────────────────────────────────────────
# 监控列表管理
# ──────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)


def load_watchlist() -> list[str]:
    _ensure_dir()
    if not os.path.exists(WATCHLIST_PATH):
        return []
    with open(WATCHLIST_PATH) as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_watchlist(domains: list[str]):
    _ensure_dir()
    with open(WATCHLIST_PATH, "w") as f:
        json.dump(sorted(set(domains)), f, ensure_ascii=False, indent=2)


def cmd_add(domains: list[str]):
    existing = load_watchlist()
    added = []
    for d in domains:
        d = _normalize_domain(d)
        if d not in existing:
            existing.append(d)
            added.append(d)
    save_watchlist(existing)
    if added:
        print(f"✅ 已添加 {len(added)} 个域名:")
        for d in added:
            print(f"   + {d}")
    else:
        print("ℹ️  所有域名已在监控列表中，无需重复添加")
    print(f"📋 当前监控列表共 {len(load_watchlist())} 个域名")


def cmd_remove(domains: list[str]):
    existing = load_watchlist()
    removed = []
    for d in domains:
        d = _normalize_domain(d)
        if d in existing:
            existing.remove(d)
            removed.append(d)
    save_watchlist(existing)
    if removed:
        print(f"🗑️  已移除 {len(removed)} 个域名:")
        for d in removed:
            print(f"   - {d}")
    else:
        print("ℹ️  指定域名不在监控列表中")
    print(f"📋 当前监控列表共 {len(load_watchlist())} 个域名")


def cmd_list():
    domains = load_watchlist()
    if not domains:
        print("📋 监控列表为空，使用 `add <域名>` 添加")
        return
    print(f"📋 监控列表（共 {len(domains)} 个域名）:")
    for d in domains:
        print(f"   • {d}")


def _normalize_domain(host: str) -> str:
    """去除协议前缀并去除末尾斜杠，检测时由 check_domain 统一补 https://"""
    host = host.strip().rstrip("/")
    if "://" in host:
        host = host.split("://", 1)[1]
    return host


def _label(share_key: Optional[str], host: str) -> str:
    """并发/日志前缀：远程配置时带上 share key，便于对照配置中心。"""
    if share_key:
        return f"{share_key} | {host}"
    return host


def load_synced_jobs() -> List[Tuple[Optional[str], str]]:
    """
    读取 sync 写入的 synced_map.json（key + 域名）。
    无文件或解析失败时返回空列表（由 run --synced 提示先 sync）。
    """
    if not os.path.exists(SYNCED_MAP_PATH):
        return []
    try:
        with open(SYNCED_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: List[Tuple[Optional[str], str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        dom = item.get("domain")
        if not isinstance(dom, str) or not dom.strip():
            continue
        k = item.get("key")
        if k is not None and not isinstance(k, str):
            k = str(k)
        out.append((k, _normalize_domain(dom)))
    return out


def _read_domains_from_file(path: str) -> list[str]:
    """从文件读取域名列表，每行一个，忽略空行和 # 注释"""
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        sys.exit(1)
    domains = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.append(line)
    return domains


def _read_jobs_from_file(path: str) -> List[Tuple[Optional[str], str]]:
    """
    检测用：每行一个任务。
    - 仅域名：`example.com`
    - 带配置 key（制表符分隔）：`{share.xxx}\\texample.com`（便于非 JSON 的手工/导出列表）
    """
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        sys.exit(1)
    out: List[Tuple[Optional[str], str]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                key, dom = line.split("\t", 1)
                key = key.strip() or None
                out.append((key, _normalize_domain(dom.strip())))
            else:
                out.append((None, _normalize_domain(line)))
    return out


# ──────────────────────────────────────────────
# 远程 JSON 同步
# ──────────────────────────────────────────────

def _load_sync_meta() -> dict:
    if not os.path.exists(SYNC_META_PATH):
        return {}
    try:
        with open(SYNC_META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sync_meta(url: str, count: int, batch_size: int = 0):
    _ensure_dir()
    meta = {
        "url": url,
        "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": count,
        "batch_size": batch_size,  # 0 表示不分批
    }
    with open(SYNC_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _fetch_remote_text(url: str) -> str:
    """HTTP GET 拉取远程文本；失败则间隔重试，共 SYNC_FETCH_ATTEMPTS 次。"""
    last_err: Optional[Exception] = None
    for attempt in range(SYNC_FETCH_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            last_err = e
            n = attempt + 1
            if n < SYNC_FETCH_ATTEMPTS:
                print(f"⚠️  拉取失败（第 {n}/{SYNC_FETCH_ATTEMPTS} 次）: {e}")
                print(f"   {SYNC_RETRY_DELAY_S}s 后重试…")
                time.sleep(SYNC_RETRY_DELAY_S)
            else:
                print(f"❌ 拉取失败（已尝试 {SYNC_FETCH_ATTEMPTS} 次）: {last_err}")
                sys.exit(1)
    assert False, "unreachable"


def cmd_sync(url: str, force: bool = False, batch_size: int = 0):
    """
    拉取远程 JSON，写入 synced_map.json。
    JSON 格式示例：{"{share.xxx}": "domain.cc", ...}
    batch_size > 0 时记录分批大小，run --synced 检测时按批逐批执行。
    """
    # 检查距上次同步是否未满 10 分钟（非强制模式）
    if not force:
        meta = _load_sync_meta()
        if meta.get("url") == url:
            last_ts = meta.get("last_sync", "")
            try:
                last_t = time.mktime(time.strptime(last_ts, "%Y-%m-%d %H:%M:%S"))
                elapsed = time.time() - last_t
                if elapsed < 600:
                    remaining = int(600 - elapsed)
                    print(f"⏭️  距上次同步仅 {int(elapsed)}s，跳过（还需 {remaining}s 后才满 10 分钟）")
                    print(f"   上次: {last_ts}，共 {meta.get('count', 0)} 个域名")
                    print(f"   使用 --force 强制重新拉取")
                    return
            except Exception:
                pass

    print(f"🌐 拉取远程配置: {url}")
    content = _fetch_remote_text(url)

    try:
        data = json.loads(content)
    except Exception as e:
        print(f"❌ JSON 解析失败: {e}")
        sys.exit(1)

    if not isinstance(data, dict):
        print("❌ JSON 格式不符（期望顶层为对象）")
        sys.exit(1)

    # 保留 key -> 域名（用于 run --synced 时在输出中标注配置 key，如 {share.xxx}）
    entries: list[dict[str, str]] = []
    for k, v in data.items():
        if not isinstance(v, str) or not v.strip():
            continue
        entries.append({"key": str(k), "domain": _normalize_domain(v)})
    entries.sort(key=lambda x: x["key"])

    _ensure_dir()
    with open(SYNCED_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    _save_sync_meta(url, len(entries), batch_size)
    batch_info = f"，每批 {batch_size} 个（共 {-(-len(entries)//batch_size)} 批）" if batch_size > 0 else ""
    print(f"✅ 同步完成，共 {len(entries)} 条{batch_info} → {SYNCED_MAP_PATH}")


# ──────────────────────────────────────────────
# Chrome / Selenium
# ──────────────────────────────────────────────

def _clear_proxy():
    """清除代理环境变量（主线程调用，非线程安全）"""
    cleared = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        val = os.environ.pop(k, None)
        if val:
            cleared[k] = val
    return cleared


def _restore_proxy(saved: dict):
    os.environ.update(saved)


def _staggered_chrome_start():
    """错峰启动：确保两次 Chrome 启动之间有间隔，减少对 itdog.cn 的冲击"""
    global _last_launch_time
    with _launch_lock:
        now  = time.time()
        gap  = LAUNCH_GAP_MIN + random.uniform(0, LAUNCH_GAP_JITTER)
        wait = (_last_launch_time + gap) - now
        if wait > 0:
            time.sleep(wait)
        _last_launch_time = time.time()


def _get_driver(temp_dir: str):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        print("❌ 缺少依赖: pip install selenium")
        sys.exit(1)

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-proxy-server")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-data-dir={temp_dir}")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


# ──────────────────────────────────────────────
# 核心检测逻辑
# ──────────────────────────────────────────────

def check_domain(host: str, verbose: bool, threshold: float,
                 overseas: bool = False, prefix: str = "", share_key: Optional[str] = None) -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    host_url = host if "://" in host else "https://" + host
    display = prefix if prefix else _label(share_key, host)
    _safe_print(f"\n🔍 检测: {host_url}", display)

    # 错峰启动，避免多 Chrome 同时冲击 itdog.cn
    _staggered_chrome_start()

    temp_dir = tempfile.mkdtemp(prefix="chrome_itdog_")
    driver   = None

    try:
        driver = _get_driver(temp_dir)

        hook_js = """
            window._itdog_finished = false;
            window._itdog_nodes    = [];
            var _OrigWS = window.WebSocket;
            window.WebSocket = new Proxy(_OrigWS, {
                construct: function(target, args) {
                    var ws = new target(...args);
                    ws.addEventListener('message', function(e) {
                        try {
                            var d = JSON.parse(e.data);
                            if (d && d.type === 'success') {
                                window._itdog_nodes.push({
                                    name:      d.name      || '',
                                    ip:        d.ip        || '',
                                    http_code: d.http_code || 0,
                                    time:      d.all_time  || 0
                                });
                            }
                            if (d && d.type === 'finished') {
                                window._itdog_finished = true;
                            }
                        } catch(ex) {}
                    });
                    return ws;
                }
            });
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': hook_js})
        driver.get("https://www.itdog.cn/http/")

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "host"))
        )
        time.sleep(2)

        overseas_js = "true" if overseas else "false"
        driver.execute_script(f"""
            var overseasCb = document.querySelector('input[name="line"][value="5"]');
            if (overseasCb) {{
                if ({overseas_js} && !overseasCb.checked) {{
                    overseasCb.click();
                }} else if (!{overseas_js} && overseasCb.checked) {{
                    overseasCb.click();
                }}
            }}
            var el = document.getElementById('host');
            el.value = '{host_url}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        """)
        lines_desc = "电信+联通+移动+海外" if overseas else "电信+联通+移动"
        _safe_print(f"📡 检测节点: {lines_desc}", display)
        driver.find_element(By.CSS_SELECTOR, "button[onclick*=\"check_form('fast')\"]").click()

        _safe_print("⏳ 等待 WebSocket 检测完成...", display)
        deadline = time.time() + 120
        while time.time() < deadline:
            finished = driver.execute_script("return window._itdog_finished;")
            count    = driver.execute_script("return (window._itdog_nodes||[]).length;")
            if finished:
                _safe_print(f"✅ 接收完成，共 {count} 个节点", display)
                break
            if count:
                _safe_print(f"   已收到 {count} 个节点，等待完成...", display)
            time.sleep(5)
        else:
            count = driver.execute_script("return (window._itdog_nodes||[]).length;")
            _safe_print(f"⚠️  超时，当前已收到 {count} 个节点", display)

        result_json = driver.execute_script("return JSON.stringify(window._itdog_nodes || []);")
        nodes = json.loads(result_json or "[]")

        if verbose and nodes:
            lines = []
            for n in nodes:
                icon = "✅" if n.get("http_code") == 200 else "❌"
                lines.append(f"  {icon} {n.get('name',''):<12} | IP: {n.get('ip',''):<15} | HTTP: {n.get('http_code')}")
            _safe_print("\n".join(lines), display)

        total   = len(nodes)
        ok      = sum(1 for n in nodes if n.get("http_code") == 200)
        rate    = ok / total if total > 0 else 0.0
        blocked = (rate < threshold) if total > 0 else None

        return {
            "host": host,
            "share_key": share_key,
            "blocked": blocked,
            "rate": rate,
            "ok": ok,
            "total": total,
            "nodes": nodes,
        }

    except Exception as e:
        _safe_print(f"❌ 检测异常: {e}", display)
        return {"host": host, "share_key": share_key, "blocked": None, "error": str(e)}

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        # driver.quit() 不能保证杀死所有 Chrome 子进程，用 temp_dir 精确兜底
        try:
            subprocess.run(["pkill", "-f", temp_dir], capture_output=True, timeout=5)
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 并发执行
# ──────────────────────────────────────────────

def _normalize_jobs(items: List[Any]) -> List[Tuple[Optional[str], str]]:
    if not items:
        return []
    if isinstance(items[0], str):
        return [(None, d) for d in items]
    return items


def _print_progress(done: int, total: int, progress_every: int):
    if progress_every and done > 0 and done % progress_every == 0:
        print(f"📍 进度: 已完成 {done}/{total}")


def run_checks(
    items: list,
    verbose: bool,
    threshold: float,
    overseas: bool,
    concurrency: int,
    progress_every: int = 10,
) -> list[dict]:
    """
    并发检测多个域名。
    items 可为 list[str]，或 list[tuple[share_key|None, domain]]（远程 sync 后的 key 映射）。
    - concurrency=1 退化为串行
    - concurrency>1 各线程输出加前缀（含 share key），结果按原始顺序返回
    - progress_every：每完成 N 个域名打印一次进度（0 表示关闭）
    """
    jobs = _normalize_jobs(items)
    total = len(jobs)
    if total == 0:
        return []

    if total > 1:
        print(f"📋 共 {total} 个域名待检测")

    if concurrency <= 1 or total <= 1:
        saved = _clear_proxy()
        results: list[dict] = []
        try:
            for i, (key, d) in enumerate(jobs):
                display = _label(key, d)
                r = check_domain(
                    d, verbose, threshold, overseas,
                    prefix=display, share_key=key,
                )
                results.append(r)
                _print_progress(i + 1, total, progress_every)
            if progress_every and total % progress_every != 0:
                print(f"📍 进度: 已完成 {total}/{total}")
            return results
        finally:
            _restore_proxy(saved)

    actual = min(concurrency, MAX_CONCURRENCY, total)
    print(f"⚡ 并发检测 {total} 个域名（{actual} 并发）\n")

    saved = _clear_proxy()
    results_by_index: List[Optional[dict]] = [None] * total
    progress_lock = threading.Lock()
    completed = [0]

    def _one(key: Optional[str], d: str) -> dict:
        display = _label(key, d)
        return check_domain(
            d, verbose, threshold, overseas,
            prefix=display, share_key=key,
        )

    try:
        with ThreadPoolExecutor(max_workers=actual) as pool:
            future_to_idx: dict = {}
            for idx, (key, d) in enumerate(jobs):
                fut = pool.submit(_one, key, d)
                future_to_idx[fut] = idx

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                key, d = jobs[idx]
                try:
                    results_by_index[idx] = future.result()
                except Exception as e:
                    results_by_index[idx] = {
                        "host": d,
                        "share_key": key,
                        "blocked": None,
                        "error": str(e),
                    }
                with progress_lock:
                    completed[0] += 1
                    _print_progress(completed[0], total, progress_every)
    finally:
        _restore_proxy(saved)

    if progress_every and completed[0] % progress_every != 0:
        print(f"📍 进度: 已完成 {total}/{total}")

    return [r for r in results_by_index if r is not None]


# ──────────────────────────────────────────────
# 分批检测
# ──────────────────────────────────────────────

def run_checks_batched(
    items: list,
    verbose: bool,
    threshold: float,
    overseas: bool,
    concurrency: int,
    progress_every: int = 10,
    batch_size: int = 0,
    batch_delay: float = 30.0,
) -> list[dict]:
    """
    分批执行检测。batch_size <= 0 时退化为单批（等同于 run_checks）。
    每批结束后等待 batch_delay 秒再进行下一批，最后一批不等待。
    """
    jobs = _normalize_jobs(items)
    total = len(jobs)

    if batch_size <= 0 or total <= batch_size:
        return run_checks(jobs, verbose, threshold, overseas, concurrency, progress_every)

    batches = [jobs[i:i + batch_size] for i in range(0, total, batch_size)]
    total_batches = len(batches)
    print(f"📦 共 {total} 个域名，分 {total_batches} 批（每批最多 {batch_size} 个）")

    all_results: list[dict] = []
    for batch_idx, batch in enumerate(batches, 1):
        print(f"\n{'─' * 48}")
        print(f"📦 第 {batch_idx}/{total_batches} 批，共 {len(batch)} 个域名")
        print(f"{'─' * 48}")
        results = run_checks(batch, verbose, threshold, overseas, concurrency, progress_every)
        all_results.extend(results)
        if batch_idx < total_batches:
            print(f"\n⏸️  批次间隔 {batch_delay:.0f}s，等待中...")
            time.sleep(batch_delay)

    return all_results


# ──────────────────────────────────────────────
# 输出 & 日志
# ──────────────────────────────────────────────

def _result_label(r: dict) -> str:
    k = r.get("share_key")
    h = r.get("host", "")
    if k:
        return f"{k} → {h}"
    return h


def print_summary(results: list):
    print("\n" + "═" * 56)
    print("📊 检测汇总")
    print("═" * 56)
    blocked_list = []
    for r in results:
        lab = _result_label(r)
        if r.get("error"):
            print(f"  ⚠️  {lab} — 检测失败: {r['error']}")
        elif r.get("blocked") is None:
            print(f"  ⚠️  {lab} — 无节点数据")
        elif r["blocked"]:
            print(f"  🚫 {lab} — 疑似被封  (成功率 {r['rate']*100:.1f}%，{r['ok']}/{r['total']} 节点)")
            blocked_list.append(lab)
        else:
            print(f"  ✅ {lab} — 正常访问  (成功率 {r['rate']*100:.1f}%，{r['ok']}/{r['total']} 节点)")
    print("═" * 56)
    if blocked_list:
        print(f"🚨 发现 {len(blocked_list)} 个疑似被封域名:")
        for d in blocked_list:
            print(f"   - {d}")
    else:
        print("🎉 所有域名均可正常访问")
    return blocked_list


def _append_log(results: list):
    _ensure_dir()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*56}\n{ts}\n{'='*56}\n")
        for r in results:
            lab = _result_label(r)
            if r.get("error"):
                f.write(f"  ERROR   {lab}: {r['error']}\n")
            elif r.get("blocked") is None:
                f.write(f"  NO_DATA {lab}\n")
            elif r["blocked"]:
                f.write(f"  BLOCKED {lab}  ({r['rate']*100:.1f}% {r['ok']}/{r['total']})\n")
            else:
                f.write(f"  OK      {lab}  ({r['rate']*100:.1f}% {r['ok']}/{r['total']})\n")


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("add", "remove", "list", "run", "sync"):
        subcmd = sys.argv[1]

        if subcmd == "list":
            cmd_list()
            return

        if subcmd == "add":
            sub_parser = argparse.ArgumentParser()
            sub_parser.add_argument("subcmd")
            sub_parser.add_argument("domains", nargs="*")
            sub_parser.add_argument("--file", "-f", help="从文件读取域名（每行一个）")
            args = sub_parser.parse_args()
            domains = list(args.domains)
            if args.file:
                domains += _read_domains_from_file(args.file)
            if not domains:
                print("用法: check_domain.py add <域名1> [域名2] ... [-f 文件路径]")
                sys.exit(1)
            cmd_add(domains)
            return

        if subcmd == "remove":
            sub_parser = argparse.ArgumentParser()
            sub_parser.add_argument("subcmd")
            sub_parser.add_argument("domains", nargs="*")
            sub_parser.add_argument("--file", "-f", help="从文件读取域名（每行一个）")
            args = sub_parser.parse_args()
            domains = list(args.domains)
            if args.file:
                domains += _read_domains_from_file(args.file)
            if not domains:
                print("用法: check_domain.py remove <域名1> [域名2] ... [-f 文件路径]")
                sys.exit(1)
            cmd_remove(domains)
            return

        if subcmd == "sync":
            sub_parser = argparse.ArgumentParser()
            sub_parser.add_argument("subcmd")
            sub_parser.add_argument("url", nargs="?", help="远程 JSON 配置地址")
            sub_parser.add_argument("--force", action="store_true", help="忽略 10 分钟冷却，强制重新拉取")
            sub_parser.add_argument(
                "--batch-size", "-B",
                type=int,
                default=0,
                metavar="N",
                help="记录分批大小，run --synced 检测时按此批次逐批执行（0 = 不分批）",
            )
            args = sub_parser.parse_args()
            if not args.url:
                # 尝试从上次元数据读取 url
                meta = _load_sync_meta()
                if meta.get("url"):
                    args.url = meta["url"]
                    print(f"ℹ️  使用上次同步地址: {args.url}")
                else:
                    print("用法: check_domain.py sync <url> [--batch-size N] [--force]")
                    sys.exit(1)
            cmd_sync(args.url, force=args.force, batch_size=args.batch_size)
            return

        if subcmd == "run":
            sub_parser = argparse.ArgumentParser()
            sub_parser.add_argument("subcmd")
            sub_parser.add_argument("--verbose",     "-v", action="store_true")
            sub_parser.add_argument("--overseas",    "-o", action="store_true")
            sub_parser.add_argument("--threshold",   type=float, default=DEFAULT_THRESHOLD)
            sub_parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                                    help=f"并发数（默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY}）")
            sub_parser.add_argument(
                "--file",
                "-f",
                help="从文件读取任务：每行一个域名；可选 key\\t域名（制表符）",
            )
            sub_parser.add_argument("--synced", "-s", action="store_true",
                                    help=f"使用 sync 拉取的本地缓存（优先 {SYNCED_MAP_PATH} 含 key）")
            sub_parser.add_argument(
                "--progress-every",
                type=int,
                default=10,
                metavar="N",
                help="每完成 N 个域名打印一次进度（默认 10，设为 0 关闭）",
            )
            sub_parser.add_argument(
                "--batch-size", "-B",
                type=int,
                default=0,
                metavar="N",
                help="分批检测，每批 N 个域名（默认 0 = 不分批）",
            )
            sub_parser.add_argument(
                "--batch-delay",
                type=float,
                default=30.0,
                metavar="S",
                help="批次间隔秒数（默认 30s）",
            )
            args = sub_parser.parse_args()
            if args.synced:
                items = load_synced_jobs()
                if not items:
                    print(f"❌ 未找到同步数据或列表为空: {SYNCED_MAP_PATH}")
                    print("   请先执行: check_domain.py sync <url>   或   sync --force")
                    sys.exit(1)
                # 若 CLI 未显式指定 batch-size，从 sync 时记录的 meta 读取
                if args.batch_size == 0:
                    meta_bs = _load_sync_meta().get("batch_size", 0)
                    if meta_bs and meta_bs > 0:
                        args.batch_size = meta_bs
                        print(f"ℹ️  使用 sync 时记录的分批大小: {meta_bs} 个/批")
            elif args.file:
                items = _read_jobs_from_file(args.file)
            else:
                items = load_watchlist()
            if not items:
                print("❌ 监控列表为空，请先用 `add <域名>` 添加域名，或用 -f 指定文件")
                sys.exit(1)
            print(f"🕐 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            results = run_checks_batched(
                items,
                args.verbose,
                args.threshold,
                args.overseas,
                args.concurrency,
                progress_every=args.progress_every,
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
            )
            print_summary(results)
            _append_log(results)
            print(f"\n🕐 完成: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            sys.exit(1 if any(r.get("blocked") for r in results) else 0)

    # 兼容旧用法：直接传域名参数（一次性检测）
    parser = argparse.ArgumentParser(
        description="域名封锁检测（via itdog.cn headless Chrome）",
        epilog="子命令: add / remove / list / run"
    )
    parser.add_argument("domains",       nargs="*",  help="要检测的域名（一次性，不写入监控列表）")
    parser.add_argument(
        "--file",
        "-f",
        help="从文件读取：每行一个域名；可选 key\\t域名（制表符），与命令行域名合并",
    )
    parser.add_argument("--verbose",     "-v", action="store_true", help="显示各节点详情")
    parser.add_argument("--overseas",    "-o", action="store_true", help="包含港澳台、海外节点")
    parser.add_argument("--threshold",   type=float, default=DEFAULT_THRESHOLD, help="封锁判定阈值（默认 0.7）")
    parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"并发数（默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY}）")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        metavar="N",
        help="每完成 N 个域名打印一次进度（默认 10，设为 0 关闭）",
    )
    args = parser.parse_args()

    items: List[Tuple[Optional[str], str]] = [
        (None, _normalize_domain(d)) for d in args.domains
    ]
    if args.file:
        items += _read_jobs_from_file(args.file)
    if not items:
        parser.print_help()
        sys.exit(1)

    print(f"🕐 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    results = run_checks(
        items,
        args.verbose,
        args.threshold,
        args.overseas,
        args.concurrency,
        progress_every=args.progress_every,
    )
    print_summary(results)
    print(f"\n🕐 完成: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.exit(1 if any(r.get("blocked") for r in results) else 0)


if __name__ == "__main__":
    main()