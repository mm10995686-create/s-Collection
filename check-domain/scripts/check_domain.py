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
  <域名...>           一次性检测指定域名（不影响监控列表）
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
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_THRESHOLD   = 0.7
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY     = 5
WATCHLIST_PATH      = os.path.expanduser("~/.openclaw/data/check-domain/watchlist.json")
LOG_PATH            = os.path.expanduser("~/.openclaw/data/check-domain/check.log")

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
    host = host.strip().rstrip("/")
    if "://" in host:
        host = host.split("://", 1)[1]
    return host


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
                 overseas: bool = False, prefix: str = "") -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    host_url = host if "://" in host else "https://" + host
    _safe_print(f"\n🔍 检测: {host_url}", prefix)

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
        _safe_print(f"📡 检测节点: {lines_desc}", prefix)
        driver.find_element(By.CSS_SELECTOR, "button[onclick*=\"check_form('fast')\"]").click()

        _safe_print("⏳ 等待 WebSocket 检测完成...", prefix)
        deadline = time.time() + 120
        while time.time() < deadline:
            finished = driver.execute_script("return window._itdog_finished;")
            count    = driver.execute_script("return (window._itdog_nodes||[]).length;")
            if finished:
                _safe_print(f"✅ 接收完成，共 {count} 个节点", prefix)
                break
            if count:
                _safe_print(f"   已收到 {count} 个节点，等待完成...", prefix)
            time.sleep(5)
        else:
            count = driver.execute_script("return (window._itdog_nodes||[]).length;")
            _safe_print(f"⚠️  超时，当前已收到 {count} 个节点", prefix)

        result_json = driver.execute_script("return JSON.stringify(window._itdog_nodes || []);")
        nodes = json.loads(result_json or "[]")

        if verbose and nodes:
            lines = []
            for n in nodes:
                icon = "✅" if n.get("http_code") == 200 else "❌"
                lines.append(f"  {icon} {n.get('name',''):<12} | IP: {n.get('ip',''):<15} | HTTP: {n.get('http_code')}")
            _safe_print("\n".join(lines), prefix)

        total   = len(nodes)
        ok      = sum(1 for n in nodes if n.get("http_code") == 200)
        rate    = ok / total if total > 0 else 0.0
        blocked = (rate < threshold) if total > 0 else None

        return {"host": host, "blocked": blocked, "rate": rate, "ok": ok, "total": total, "nodes": nodes}

    except Exception as e:
        _safe_print(f"❌ 检测异常: {e}", prefix)
        return {"host": host, "blocked": None, "error": str(e)}

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 并发执行
# ──────────────────────────────────────────────

def run_checks(domains: list[str], verbose: bool, threshold: float,
               overseas: bool, concurrency: int) -> list[dict]:
    """
    并发检测多个域名。
    - concurrency=1 退化为串行（输出无前缀，与旧行为一致）
    - concurrency>1 各线程输出加域名前缀，结果按原始顺序返回
    """
    if concurrency <= 1 or len(domains) <= 1:
        saved = _clear_proxy()
        try:
            return [check_domain(d, verbose, threshold, overseas) for d in domains]
        finally:
            _restore_proxy(saved)

    actual = min(concurrency, MAX_CONCURRENCY, len(domains))
    print(f"⚡ 并发检测 {len(domains)} 个域名（{actual} 并发）\n")

    saved = _clear_proxy()
    results_map: dict[str, dict] = {}
    try:
        with ThreadPoolExecutor(max_workers=actual) as pool:
            future_to_host = {
                pool.submit(check_domain, d, verbose, threshold, overseas, d): d
                for d in domains
            }
            for future in as_completed(future_to_host):
                host = future_to_host[future]
                try:
                    results_map[host] = future.result()
                except Exception as e:
                    results_map[host] = {"host": host, "blocked": None, "error": str(e)}
    finally:
        _restore_proxy(saved)

    # 保持原始顺序
    return [results_map[d] for d in domains]


# ──────────────────────────────────────────────
# 输出 & 日志
# ──────────────────────────────────────────────

def print_summary(results: list):
    print("\n" + "═" * 56)
    print("📊 检测汇总")
    print("═" * 56)
    blocked_list = []
    for r in results:
        if r.get("error"):
            print(f"  ⚠️  {r['host']} — 检测失败: {r['error']}")
        elif r.get("blocked") is None:
            print(f"  ⚠️  {r['host']} — 无节点数据")
        elif r["blocked"]:
            print(f"  🚫 {r['host']} — 疑似被封  (成功率 {r['rate']*100:.1f}%，{r['ok']}/{r['total']} 节点)")
            blocked_list.append(r["host"])
        else:
            print(f"  ✅ {r['host']} — 正常访问  (成功率 {r['rate']*100:.1f}%，{r['ok']}/{r['total']} 节点)")
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
            if r.get("error"):
                f.write(f"  ERROR   {r['host']}: {r['error']}\n")
            elif r.get("blocked") is None:
                f.write(f"  NO_DATA {r['host']}\n")
            elif r["blocked"]:
                f.write(f"  BLOCKED {r['host']}  ({r['rate']*100:.1f}% {r['ok']}/{r['total']})\n")
            else:
                f.write(f"  OK      {r['host']}  ({r['rate']*100:.1f}% {r['ok']}/{r['total']})\n")


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────

def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("add", "remove", "list", "run"):
        subcmd = sys.argv[1]

        if subcmd == "list":
            cmd_list()
            return

        if subcmd == "add":
            if len(sys.argv) < 3:
                print("用法: check_domain.py add <域名1> [域名2] ...")
                sys.exit(1)
            cmd_add(sys.argv[2:])
            return

        if subcmd == "remove":
            if len(sys.argv) < 3:
                print("用法: check_domain.py remove <域名1> [域名2] ...")
                sys.exit(1)
            cmd_remove(sys.argv[2:])
            return

        if subcmd == "run":
            sub_parser = argparse.ArgumentParser()
            sub_parser.add_argument("subcmd")
            sub_parser.add_argument("--verbose",     "-v", action="store_true")
            sub_parser.add_argument("--overseas",    "-o", action="store_true")
            sub_parser.add_argument("--threshold",   type=float, default=DEFAULT_THRESHOLD)
            sub_parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                                    help=f"并发数（默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY}）")
            args = sub_parser.parse_args()
            domains = load_watchlist()
            if not domains:
                print("❌ 监控列表为空，请先用 `add <域名>` 添加域名")
                sys.exit(1)
            print(f"🕐 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            results = run_checks(domains, args.verbose, args.threshold, args.overseas, args.concurrency)
            print_summary(results)
            _append_log(results)
            print(f"\n🕐 完成: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            sys.exit(1 if any(r.get("blocked") for r in results) else 0)

    # 兼容旧用法：直接传域名参数（一次性检测）
    parser = argparse.ArgumentParser(
        description="域名封锁检测（via itdog.cn headless Chrome）",
        epilog="子命令: add / remove / list / run"
    )
    parser.add_argument("domains",       nargs="+",  help="要检测的域名（一次性，不写入监控列表）")
    parser.add_argument("--verbose",     "-v", action="store_true", help="显示各节点详情")
    parser.add_argument("--overseas",    "-o", action="store_true", help="包含港澳台、海外节点")
    parser.add_argument("--threshold",   type=float, default=DEFAULT_THRESHOLD, help="封锁判定阈值（默认 0.7）")
    parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"并发数（默认 {DEFAULT_CONCURRENCY}，最大 {MAX_CONCURRENCY}）")
    args = parser.parse_args()

    print(f"🕐 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    results = run_checks(args.domains, args.verbose, args.threshold, args.overseas, args.concurrency)
    print_summary(results)
    print(f"\n🕐 完成: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.exit(1 if any(r.get("blocked") for r in results) else 0)


if __name__ == "__main__":
    main()