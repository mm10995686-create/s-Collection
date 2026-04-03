process.title = 'check-domain';
import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true, args: ['--no-proxy-server', '--no-sandbox', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});
const page = await ctx.newPage();
console.log('开始 goto...');
try {
  await page.goto('http://17ce.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
  console.log('goto 完成, title:', await page.title());
  const url = await page.$('#url');
  console.log('#url 存在:', url !== null);
} catch(e) {
  console.error('goto 失败:', e.message);
  // 尝试截图看页面状态
  await page.screenshot({ path: '/tmp/17ce_err.png' }).catch(() => {});
}
await browser.close();
