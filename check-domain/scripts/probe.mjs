import { chromium } from 'playwright';

const browser = await chromium.launch({
  headless: true,
  args: ['--proxy-server=socks5://127.0.0.1:11586', '--no-sandbox', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
});
const page = await ctx.newPage();
await page.goto('https://www.itdog.cn/http/', { waitUntil: 'domcontentloaded', timeout: 30000 });
await new Promise(r => setTimeout(r, 2000));
const title = await page.title();
console.log('title:', title);
const hostEl = await page.$('#host');
console.log('#host 存在:', hostEl !== null);
await page.screenshot({ path: '/tmp/itdog_snapshot.png' });
await browser.close();
console.log('截图已保存 /tmp/itdog_snapshot.png');
