import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});

// 拦截 WS 和 API
const page = await ctx.newPage();
page.on('websocket', ws => {
  console.log('\n=== WS 连接:', ws.url());
  ws.on('framesent',     f => console.log('WS >>>', f.payload?.toString?.().slice(0, 300)));
  ws.on('framereceived', f => console.log('WS <<<', f.payload?.toString?.().slice(0, 500)));
  ws.on('close', () => console.log('WS closed'));
});
page.on('response', async res => {
  const url = res.url();
  if (url.match(/boce\.com\/(create|task|submit|check|api)/i)) {
    try { console.log('\nAPI', res.status(), url, '->', (await res.text()).slice(0, 300)); } catch {}
  }
});

await page.goto('https://www.boce.com/http', { waitUntil: 'domcontentloaded', timeout: 30000 });
await new Promise(r => setTimeout(r, 1500));

await page.fill('input[name="host"]', 'https://baidu.com');
await new Promise(r => setTimeout(r, 300));
await page.click('span.banner_submit');
console.log('已点击开始检测');

// 等待 WS 完成，最多 60s
await new Promise(r => setTimeout(r, 60000));
await browser.close();
