import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const ctx = await browser.newContext({
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
});

// 监听网络请求，找结果 API
const apiCalls = [];
const page = await ctx.newPage();
page.on('request', req => {
  const url = req.url();
  if (url.includes('boce') || url.includes('api') || url.includes('result') || url.includes('check') || url.includes('task')) {
    apiCalls.push({ method: req.method(), url });
  }
});
page.on('websocket', ws => {
  console.log('WS连接:', ws.url());
  ws.on('framesent', f => console.log('WS sent:', f.payload));
  ws.on('framereceived', f => {
    const data = typeof f.payload === 'string' ? f.payload.slice(0, 200) : '[binary]';
    console.log('WS recv:', data);
  });
});

await page.goto('https://www.boce.com/http', { waitUntil: 'domcontentloaded', timeout: 30000 });
await new Promise(r => setTimeout(r, 2000));

// 填写域名并提交
await page.evaluate(() => {
  const input = document.querySelector('input[name="host"]');
  input.value = 'https://baidu.com';
  input.dispatchEvent(new Event('input', { bubbles: true }));
});

// 找提交按钮
const submitBtns = await page.evaluate(() =>
  Array.from(document.querySelectorAll('input[type=button],input[type=submit],button')).map(el => ({
    tag: el.tagName, type: el.type, text: el.textContent?.trim().slice(0,30), 
    className: el.className.slice(0,60), onclick: el.getAttribute('onclick')?.slice(0,80)
  }))
);
console.log('提交按钮:', JSON.stringify(submitBtns, null, 2));

// 找含"开始"或"测速"的元素
const startEl = await page.evaluate(() => {
  const all = Array.from(document.querySelectorAll('*'));
  return all.filter(el => el.children.length === 0 && /开始|测速|检测|go|start/i.test(el.textContent || ''))
    .map(el => ({ tag: el.tagName, text: el.textContent?.trim().slice(0,30), className: el.className?.slice(0,60) }))
    .slice(0, 10);
});
console.log('开始元素:', JSON.stringify(startEl, null, 2));

await new Promise(r => setTimeout(r, 3000));
console.log('\nAPI calls so far:', JSON.stringify(apiCalls.slice(0,15), null, 2));
await browser.close();
