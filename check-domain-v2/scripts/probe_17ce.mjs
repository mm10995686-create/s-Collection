process.title = 'check-domain';
import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});

const wsFrames = [];
const page = await ctx.newPage();

page.on('websocket', ws => {
  console.log('\n=== WS 连接:', ws.url());
  ws.on('framesent',     f => wsFrames.push({ dir: '>>>', data: f.payload?.toString?.() }));
  ws.on('framereceived', f => wsFrames.push({ dir: '<<<', data: f.payload?.toString?.() }));
  ws.on('close', () => console.log('WS closed'));
});

await page.goto('http://17ce.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
await new Promise(r => setTimeout(r, 1000));

// 检查登录状态
const loginState = await page.evaluate(() => {
  const loginBtn = document.querySelector('a[href*="login"], #login, .login');
  const userInfo = document.querySelector('.user-info, .username, #username');
  return { loginBtn: loginBtn?.textContent?.trim(), userInfo: userInfo?.textContent?.trim() };
});
console.log('登录状态:', loginState);

// 填 URL
await page.fill('#url', 'https://baidu.com');
await new Promise(r => setTimeout(r, 300));

// 只选大陆：取消全部，只选大陆(1)
await page.evaluate(() => {
  // area: 只勾选大陆(1)，其他取消
  document.querySelectorAll('input[name="area"]').forEach((cb) => {
    const v = cb.value;
    const shouldCheck = v === '1'; // 只大陆
    if (cb.checked !== shouldCheck) cb.click();
  });
});
await new Promise(r => setTimeout(r, 200));

// 点击检测
await page.click('#su');
console.log('已点击检测');

// 等待 WS 完成
await new Promise(r => setTimeout(r, 60000));

// 打印所有 WS 帧
console.log('\n=== WS 帧（前20条）===');
wsFrames.slice(0, 20).forEach(f => console.log(f.dir, f.data?.slice(0, 400)));

await browser.close();
