process.title = 'check-domain';
import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});

const apiCalls = [];
const page = await ctx.newPage();
page.on('request', req => {
  const url = req.url();
  if (!url.match(/\.(css|js|png|jpg|gif|woff|ico)(\?|$)/)) {
    apiCalls.push({ method: req.method(), url: url.slice(0, 120) });
  }
});
page.on('response', async res => {
  const url = res.url();
  if (url.includes('task') || url.includes('result') || url.includes('check') || url.includes('api') || url.includes('boce.com/') && !url.match(/\.(css|js|png|jpg)/)) {
    try {
      const text = await res.text();
      if (text.length < 2000) console.log('RESP', url.slice(0,100), '->', text.slice(0, 300));
    } catch {}
  }
});

await page.goto('https://www.boce.com/http', { waitUntil: 'domcontentloaded', timeout: 30000 });
await new Promise(r => setTimeout(r, 1500));

// 填域名
await page.fill('input[name="host"]', 'https://baidu.com');
await new Promise(r => setTimeout(r, 500));

// 找并点击提交（banner区域的主按钮）
const clicked = await page.evaluate(() => {
  // 找 banner 区域的提交按钮
  const btns = Array.from(document.querySelectorAll('.banner input[type=button], .banner button, input.btn-primary'));
  for (const btn of btns) {
    const style = window.getComputedStyle(btn);
    if (style.display !== 'none' && style.visibility !== 'hidden') {
      btn.click();
      return btn.className + ' | ' + btn.value;
    }
  }
  // 备选：找所有可见的 submit 类按钮
  const all = Array.from(document.querySelectorAll('input[type=button]'));
  const first = all.find(b => {
    const s = window.getComputedStyle(b);
    return s.display !== 'none';
  });
  if (first) { first.click(); return 'fallback: ' + first.className; }
  return 'none clicked';
});
console.log('点击了:', clicked);

// 等待结果加载
await new Promise(r => setTimeout(r, 8000));

// 检查页面是否有结果
const resultInfo = await page.evaluate(() => {
  // 找结果表格或节点列表
  const rows = document.querySelectorAll('table tr, .result-item, .node-result');
  return { rowCount: rows.length, url: location.href };
});
console.log('结果信息:', resultInfo);

console.log('\n全部 API 请求:');
apiCalls.forEach(c => console.log(c.method, c.url));

await page.screenshot({ path: '/tmp/boce_after_submit.png' });
await browser.close();
