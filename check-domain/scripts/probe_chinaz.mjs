/**
 * 探针：分析 tool.chinaz.com/speedtest 的 WebSocket 协议
 * 运行: node probe_chinaz.mjs
 */
process.title = 'check-domain';
import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});

// 注入 WebSocket 拦截钩子，捕获所有消息
await ctx.addInitScript(`
  window._chinaz_msgs = [];
  window._chinaz_ws_urls = [];
  const _OrigWS = window.WebSocket;
  window.WebSocket = new Proxy(_OrigWS, {
    construct(target, args) {
      window._chinaz_ws_urls.push(args[0]);
      console.log('[WS OPEN]', args[0]);
      const ws = new target(...args);
      ws.addEventListener('message', (e) => {
        window._chinaz_msgs.push(e.data);
        console.log('[WS MSG]', e.data.slice(0, 300));
      });
      ws.addEventListener('close', (e) => {
        console.log('[WS CLOSE]', e.code, e.reason);
      });
      return ws;
    }
  });
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
`);

const page = await ctx.newPage();

// 监听控制台日志（捕获 addInitScript 里的 console.log）
page.on('console', msg => {
  if (msg.text().startsWith('[WS')) {
    process.stdout.write('  console> ' + msg.text() + '\n');
  }
});

// 监听所有网络请求（过滤静态资源）
const reqs = [];
page.on('request', req => {
  const u = req.url();
  if (!u.match(/\.(css|js|png|jpg|gif|woff|ico|svg)(\?|$)/)) {
    reqs.push({ method: req.method(), url: u.slice(0, 120) });
  }
});

// 监听 API 响应
page.on('response', async res => {
  const u = res.url();
  if (u.includes('api') || u.includes('speed') || u.includes('check') || u.includes('task') || u.includes('result')) {
    try {
      const t = await res.text();
      if (t.length < 3000 && t.length > 0) {
        console.log(`  RESP [${res.status()}] ${u.slice(0, 80)}`);
        console.log('    body:', t.slice(0, 200));
      }
    } catch {}
  }
});

const TARGET = 'baidu.com';
console.log(`\n── 直接导航到结果页 ──`);
await page.goto(`https://tool.chinaz.com/speedtest/${TARGET}`, {
  waitUntil: 'domcontentloaded',
  timeout: 30_000,
});
console.log('页面标题:', await page.title());
console.log('当前 URL:', page.url());

// 等待结果加载
console.log('\n⏳ 等待 WebSocket 数据（最多 30s）...');
const deadline = Date.now() + 30_000;
while (Date.now() < deadline) {
  const msgs = await page.evaluate(() => window._chinaz_msgs.length);
  const urls = await page.evaluate(() => window._chinaz_ws_urls);
  if (msgs > 0) {
    console.log(`  已收到 ${msgs} 条 WS 消息，WS URLs:`, urls);
    // 打印前 5 条消息内容
    const sample = await page.evaluate(() => window._chinaz_msgs.slice(0, 10));
    console.log('\n── WS 消息样本 ──');
    sample.forEach((m, i) => console.log(`  [${i}]`, m.slice(0, 400)));
    break;
  }
  await new Promise(r => setTimeout(r, 2000));
}

const finalMsgs = await page.evaluate(() => window._chinaz_msgs);
const finalUrls = await page.evaluate(() => window._chinaz_ws_urls);
console.log(`\n── 总结 ──`);
console.log('WS 连接 URLs:', finalUrls);
console.log('总消息数:', finalMsgs.length);

if (finalMsgs.length > 0) {
  // 尝试 JSON 解析所有消息，归类
  const parsed = finalMsgs.map(m => { try { return JSON.parse(m); } catch { return m; } });
  const types = new Set(parsed.map(p => typeof p === 'object' ? p?.type ?? p?.Type ?? '(no type)' : '(string)'));
  console.log('消息类型集合:', [...types]);

  console.log('\n── 完整消息（前 5 条）──');
  parsed.slice(0, 5).forEach((p, i) => {
    console.log(`[${i}]`, typeof p === 'string' ? p.slice(0, 300) : JSON.stringify(p).slice(0, 400));
  });

  if (finalMsgs.length > 5) {
    console.log(`\n── 最后 3 条消息 ──`);
    parsed.slice(-3).forEach((p, i) => {
      console.log(`[-${3 - i}]`, typeof p === 'string' ? p.slice(0, 300) : JSON.stringify(p).slice(0, 400));
    });
  }
}

console.log('\n── 全部 API 请求 ──');
reqs.forEach(r => console.log(r.method, r.url));

await page.screenshot({ path: '/tmp/chinaz_result.png' });
console.log('\n截图已保存至 /tmp/chinaz_result.png');

await browser.close();