#!/usr/bin/env python3
"""
域名封锁检测 - 通过 itdog.cn 多节点 HTTP 检测
使用 Selenium headless Chrome 模拟真实用户操作，完整走页面流程
成功率 < 70% 视为疑似被封
"""

import sys
import json
import argparse
import time
import os
import re
import tempfile
import shutil

DEFAULT_THRESHOLD = 0.7


def _clear_proxy():
    """临时清除代理环境变量，避免干扰 ChromeDriver 和目标站点"""
    cleared = {}
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        val = os.environ.pop(k, None)
        if val:
            cleared[k] = val
    return cleared


def _restore_proxy(saved: dict):
    os.environ.update(saved)


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
    opts.add_argument("--no-proxy-server")          # 绕过系统代理
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-data-dir={temp_dir}")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def check_domain(host: str, verbose: bool, threshold: float, overseas: bool = False) -> dict:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException

    host_url = host if "://" in host else "https://" + host
    print(f"\n🔍 检测: {host_url}")

    temp_dir = tempfile.mkdtemp(prefix="chrome_itdog_")
    saved_proxy = _clear_proxy()
    driver = None

    try:
        driver = _get_driver(temp_dir)

        # 1. 在页面任何脚本执行前注入 WebSocket hook（CDP 级别，最早执行）
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

        # 2. 打开检测页面（hook 已在页面脚本前生效）
        driver.get("https://www.itdog.cn/http/")

        # 3. 等页面 JS 初始化完成
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "host"))
        )
        time.sleep(2)

        # 4. 设置节点选项 + 填入域名 + 点击快速测试
        overseas_js = "true" if overseas else "false"
        driver.execute_script(f"""
            // 控制「港澳台、海外」节点（value=5）
            var overseasCb = document.querySelector('input[name="line"][value="5"]');
            if (overseasCb) {{
                if ({overseas_js} && !overseasCb.checked) {{
                    overseasCb.click();
                }} else if (!{overseas_js} && overseasCb.checked) {{
                    overseasCb.click();
                }}
            }}
            // 填入域名
            var el = document.getElementById('host');
            el.value = '{host_url}';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        """)
        lines_desc = "电信+联通+移动+海外" if overseas else "电信+联通+移动"
        print(f"📡 检测节点: {lines_desc}")
        driver.find_element(By.CSS_SELECTOR, "button[onclick*=\"check_form('fast')\"]").click()

        print("⏳ 等待 WebSocket 检测完成...")

        # 5. 等待 finished 标志（最多 120s），每 5s 打印进度
        deadline = time.time() + 120
        while time.time() < deadline:
            finished = driver.execute_script("return window._itdog_finished;")
            count    = driver.execute_script("return (window._itdog_nodes||[]).length;")
            if finished:
                print(f"✅ 接收完成，共 {count} 个节点")
                break
            if count:
                print(f"   已收到 {count} 个节点，等待完成...")
            time.sleep(5)
        else:
            count = driver.execute_script("return (window._itdog_nodes||[]).length;")
            print(f"⚠️  超时，当前已收到 {count} 个节点")

        # 6. 直接读 hook 收集的节点数据
        result_json = driver.execute_script("return JSON.stringify(window._itdog_nodes || []);")
        nodes = json.loads(result_json or "[]")

        if verbose and nodes:
            for n in nodes:
                icon = "✅" if n.get("http_code") == 200 else "❌"
                print(f"  {icon} {n.get('name',''):<12} | IP: {n.get('ip',''):<15} | HTTP: {n.get('http_code')}")

        total   = len(nodes)
        ok      = sum(1 for n in nodes if n.get("http_code") == 200)
        rate    = ok / total if total > 0 else 0.0
        blocked = (rate < threshold) if total > 0 else None

        return {"host": host, "blocked": blocked, "rate": rate, "ok": ok, "total": total, "nodes": nodes}

    except Exception as e:
        print(f"❌ 检测异常: {e}")
        return {"host": host, "blocked": None, "error": str(e)}

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        _restore_proxy(saved_proxy)


def _parse_from_map(driver) -> list:
    """备用：从页面 JS 全局变量里读节点原始数据"""
    try:
        raw = driver.execute_script("""
            // itdog 把结果存在多个地方，尝试拿到 resData 或 mydata
            if (typeof resData !== 'undefined') return JSON.stringify(resData);
            return '[]';
        """)
        items = json.loads(raw or "[]")
        nodes = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "success":
                nodes.append({
                    "name":      item.get("name", ""),
                    "ip":        item.get("ip", ""),
                    "http_code": item.get("http_code"),
                })
        return nodes
    except Exception:
        return []


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


def main():
    parser = argparse.ArgumentParser(description="域名封锁检测（via itdog.cn headless Chrome）")
    parser.add_argument("domains",     nargs="+",            help="要检测的域名")
    parser.add_argument("--verbose",   "-v", action="store_true", help="显示各节点详情")
    parser.add_argument("--overseas",  "-o", action="store_true", help="包含港澳台、海外节点（默认只检测国内三网）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="封锁判定阈值（默认 0.7）")
    args = parser.parse_args()

    print(f"🕐 开始: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    results = [check_domain(d, args.verbose, args.threshold, args.overseas) for d in args.domains]
    print_summary(results)
    print(f"\n🕐 完成: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.exit(1 if any(r.get("blocked") for r in results) else 0)


if __name__ == "__main__":
    main()