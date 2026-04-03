#!/usr/bin/env node
/**
 * 域名封锁检测 - 通过 itdog.cn 多节点 HTTP 检测
 * 使用 Playwright headless Chromium 模拟真实用户操作
 * 成功率 < 70% 视为疑似被封
 *
 * 子命令:
 *   add <域名...>       添加域名到监控列表
 *   remove <域名...>    从监控列表删除域名
 *   list                查看当前监控列表
 *
 *   run                 检测监控列表中所有域名
 *   sync <url>          从远程 JSON 拉取域名写入本地缓存
 *   <域名...>           一次性检测（不影响监控列表）
 */

import { chromium, type Browser, type Page } from 'playwright';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

// ── 常量 ────────────────────────────────────────────────────────────────────

const DEFAULT_THRESHOLD    = 0.7;
const DEFAULT_CONCURRENCY  = 3;
const MAX_CONCURRENCY      = 5;
const SYNC_FETCH_ATTEMPTS  = 4;
const SYNC_RETRY_DELAY_MS  = 5_000;

const DATA_DIR        = path.join(os.homedir(), '.openclaw', 'data', 'check-domain');
const WATCHLIST_PATH  = path.join(DATA_DIR, 'watchlist.json');
const LOG_PATH        = path.join(DATA_DIR, 'check.log');
const SYNCED_MAP_PATH = path.join(DATA_DIR, 'synced_map.json');
const SYNC_META_PATH  = path.join(DATA_DIR, 'sync_meta.json');

// ── 类型 ────────────────────────────────────────────────────────────────────

interface NodeResult {
  name: string;
  ip: string;
  http_code: number;
  time: number;
}

interface CheckResult {
  host: string;
  shareKey: string | null;
  platform?: Platform;
  blocked: boolean | null;
  rate?: number;
  ok?: number;
  total?: number;
  nodes?: NodeResult[];
  error?: string;
}

/** [shareKey | null, host] */
type Job = [string | null, string];

interface SyncMeta {
  url?: string;
  last_sync?: string;
  count?: number;
  batch_size?: number;
}

// ── 工具 ────────────────────────────────────────────────────────────────────

function ensureDir() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
}

function normalizeDomain(host: string): string {
  host = host.trim().replace(/\/$/, '');
  if (host.includes('://')) host = host.split('://')[1];
  return host;
}

function jobLabel(shareKey: string | null | undefined, host: string): string {
  return shareKey ? `${shareKey} | ${host}` : host;
}

function resultLabel(r: CheckResult): string {
  return r.shareKey ? `${r.shareKey} → ${r.host}` : r.host;
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function fmtNow(): string {
  // 格式：YYYY-MM-DD HH:MM:SS
  return new Date().toLocaleString('sv').replace('T', ' ');
}

function normalizeJobs(items: Array<string | Job>): Job[] {
  if (!items.length) return [];
  return typeof items[0] === 'string'
    ? (items as string[]).map(d => [null, d])
    : (items as Job[]);
}

// ── 监控列表 ────────────────────────────────────────────────────────────────

function loadWatchlist(): string[] {
  ensureDir();
  if (!fs.existsSync(WATCHLIST_PATH)) return [];
  try {
    const data = JSON.parse(fs.readFileSync(WATCHLIST_PATH, 'utf-8'));
    return Array.isArray(data) ? data : [];
  } catch { return []; }
}

function saveWatchlist(domains: string[]) {
  ensureDir();
  const sorted = [...new Set(domains)].sort();
  fs.writeFileSync(WATCHLIST_PATH, JSON.stringify(sorted, null, 2), 'utf-8');
}

function cmdAdd(domains: string[]) {
  const existing = loadWatchlist();
  const added: string[] = [];
  for (const d of domains) {
    const nd = normalizeDomain(d);
    if (!existing.includes(nd)) { existing.push(nd); added.push(nd); }
  }
  saveWatchlist(existing);
  if (added.length) {
    console.log(`✅ 已添加 ${added.length} 个域名:`);
    added.forEach(d => console.log(`   + ${d}`));
  } else {
    console.log('ℹ️  所有域名已在监控列表中，无需重复添加');
  }
  console.log(`📋 当前监控列表共 ${loadWatchlist().length} 个域名`);
}

function cmdRemove(domains: string[]) {
  let existing = loadWatchlist();
  const removed: string[] = [];
  for (const d of domains) {
    const nd = normalizeDomain(d);
    const idx = existing.indexOf(nd);
    if (idx !== -1) { existing.splice(idx, 1); removed.push(nd); }
  }
  saveWatchlist(existing);
  if (removed.length) {
    console.log(`🗑️  已移除 ${removed.length} 个域名:`);
    removed.forEach(d => console.log(`   - ${d}`));
  } else {
    console.log('ℹ️  指定域名不在监控列表中');
  }
  console.log(`📋 当前监控列表共 ${loadWatchlist().length} 个域名`);
}

function cmdList() {
  const domains = loadWatchlist();
  if (!domains.length) {
    console.log('📋 监控列表为空，使用 `add <域名>` 添加');
    return;
  }
  console.log(`📋 监控列表（共 ${domains.length} 个域名）:`);
  domains.forEach(d => console.log(`   • ${d}`));
}

// ── 远程同步 ────────────────────────────────────────────────────────────────

function loadSyncMeta(): SyncMeta {
  if (!fs.existsSync(SYNC_META_PATH)) return {};
  try { return JSON.parse(fs.readFileSync(SYNC_META_PATH, 'utf-8')); }
  catch { return {}; }
}

function saveSyncMeta(url: string, count: number, batchSize = 0) {
  ensureDir();
  const meta: SyncMeta = { url, last_sync: fmtNow(), count, batch_size: batchSize };
  fs.writeFileSync(SYNC_META_PATH, JSON.stringify(meta, null, 2), 'utf-8');
}

async function fetchRemoteText(url: string): Promise<string> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < SYNC_FETCH_ATTEMPTS; attempt++) {
    try {
      const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    } catch (e) {
      lastErr = e;
      const n = attempt + 1;
      if (n < SYNC_FETCH_ATTEMPTS) {
        console.log(`⚠️  拉取失败（第 ${n}/${SYNC_FETCH_ATTEMPTS} 次）: ${e}`);
        console.log(`   ${SYNC_RETRY_DELAY_MS / 1000}s 后重试…`);
        await sleep(SYNC_RETRY_DELAY_MS);
      }
    }
  }
  console.error(`❌ 拉取失败（已尝试 ${SYNC_FETCH_ATTEMPTS} 次）: ${lastErr}`);
  process.exit(1);
}

async function cmdSync(url: string, force = false, batchSize = 0) {
  if (!force) {
    const meta = loadSyncMeta();
    if (meta.url === url && meta.last_sync) {
      const elapsed = (Date.now() - new Date(meta.last_sync).getTime()) / 1000;
      if (elapsed < 600) {
        const remaining = Math.ceil(600 - elapsed);
        console.log(`⏭️  距上次同步仅 ${Math.floor(elapsed)}s，跳过（还需 ${remaining}s 后才满 10 分钟）`);
        console.log(`   上次: ${meta.last_sync}，共 ${meta.count ?? 0} 个域名`);
        console.log(`   使用 --force 强制重新拉取`);
        return;
      }
    }
  }

  console.log(`🌐 拉取远程配置: ${url}`);
  const content = await fetchRemoteText(url);

  let data: unknown;
  try { data = JSON.parse(content); }
  catch (e) { console.error(`❌ JSON 解析失败: ${e}`); process.exit(1); }

  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    console.error('❌ JSON 格式不符（期望顶层为对象）'); process.exit(1);
  }

  const entries: Array<{ key: string; domain: string }> = [];
  for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
    if (typeof v === 'string' && v.trim()) {
      entries.push({ key: String(k), domain: normalizeDomain(v) });
    }
  }
  entries.sort((a, b) => a.key.localeCompare(b.key));

  ensureDir();
  fs.writeFileSync(SYNCED_MAP_PATH, JSON.stringify(entries, null, 2), 'utf-8');
  saveSyncMeta(url, entries.length, batchSize);

  const batchInfo = batchSize > 0
    ? `，每批 ${batchSize} 个（共 ${Math.ceil(entries.length / batchSize)} 批）`
    : '';
  console.log(`✅ 同步完成，共 ${entries.length} 条${batchInfo} → ${SYNCED_MAP_PATH}`);
}

function loadSyncedJobs(): Job[] {
  if (!fs.existsSync(SYNCED_MAP_PATH)) return [];
  try {
    const data = JSON.parse(fs.readFileSync(SYNCED_MAP_PATH, 'utf-8'));
    if (!Array.isArray(data)) return [];
    return data
      .filter((item): item is { key?: unknown; domain: string } =>
        typeof item === 'object' && item !== null &&
        typeof (item as any).domain === 'string' && (item as any).domain.trim())
      .map(item => [
        item.key != null ? String(item.key) : null,
        normalizeDomain(item.domain),
      ]);
  } catch { return []; }
}

function readDomainsFromFile(filePath: string): string[] {
  if (!fs.existsSync(filePath)) {
    console.error(`❌ 文件不存在: ${filePath}`); process.exit(1);
  }
  return fs.readFileSync(filePath, 'utf-8')
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('#'));
}

function readJobsFromFile(filePath: string): Job[] {
  if (!fs.existsSync(filePath)) {
    console.error(`❌ 文件不存在: ${filePath}`); process.exit(1);
  }
  return fs.readFileSync(filePath, 'utf-8')
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('#'))
    .map(l => {
      if (l.includes('\t')) {
        const [k, d] = l.split('\t', 2);
        return [k.trim() || null, normalizeDomain(d.trim())] as Job;
      }
      return [null, normalizeDomain(l)] as Job;
    });
}

// ── 核心检测 ────────────────────────────────────────────────────────────────

type Platform = 'itdog' | '17ce' | 'chinaz';

// ── Hook 脚本（各平台独立，绑定 Context，每次导航自动重置） ─────────────────

const ITDOG_HOOK = /* js */ `
  window._itdog_finished = false;
  window._itdog_nodes    = [];
  const _OrigWS = window.WebSocket;
  window.WebSocket = new Proxy(_OrigWS, {
    construct(target, args) {
      const ws = new target(...args);
      ws.addEventListener('message', (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d?.type === 'success') {
            window._itdog_nodes.push({
              name: d.name || '', ip: d.ip || '',
              http_code: d.http_code || 0, time: d.all_time || 0,
            });
          }
          if (d?.type === 'finished') window._itdog_finished = true;
        } catch {}
      });
      return ws;
    }
  });
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
`;

// tool.chinaz.com/speedtest：code=1 为节点结果，code=2 为检测结束标志
const CHINAZ_HOOK = /* js */ `
  window._chinaz_finished = false;
  window._chinaz_nodes    = [];
  const _OrigWS = window.WebSocket;
  window.WebSocket = new Proxy(_OrigWS, {
    construct(target, args) {
      const ws = new target(...args);
      ws.addEventListener('message', (e) => {
        try {
          const d = JSON.parse(e.data);
          // code=1：单节点结果，包含 address/ip/httpCode/timeTotal
          if (d?.code === 1 && d?.address) {
            window._chinaz_nodes.push({
              name: d.address || '',
              ip:   d.ip      || '',
              http_code: d.httpCode  || 0,
              time:      parseInt(d.timeTotal) || 0,
            });
          }
          // code=2：全部节点检测完成
          if (d?.code === 2) window._chinaz_finished = true;
        } catch {}
      });
      return ws;
    }
  });
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
`;

// 17ce.com：NewData = 单节点结果，TaskEnd = 检测完成（含汇总计数）
const CE17_HOOK = /* js */ `
  window._17ce_finished  = false;
  window._17ce_nodes     = [];
  const _OrigWS = window.WebSocket;
  window.WebSocket = new Proxy(_OrigWS, {
    construct(target, args) {
      const ws = new target(...args);
      ws.addEventListener('message', (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d?.type === 'NewData' && d?.data) {
            const n = d.data;
            window._17ce_nodes.push({
              name: String(n.NodeID || ''), ip: '',
              http_code: n.HttpCode || 0, time: n.TotalTime || 0,
            });
          }
          if (d?.type === 'TaskEnd') window._17ce_finished = true;
        } catch {}
      });
      return ws;
    }
  });
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
`;

// ── 各平台检测函数 ────────────────────────────────────────────────────────────

async function checkItdog(
  page: Page,
  hostUrl: string,
  overseas: boolean,
  display: string,
): Promise<NodeResult[]> {
  const log = (msg: string) => console.log(`[${display}] ${msg}`);

  await page.goto('https://www.itdog.cn/http/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  const accessBtn = await page.$('#access');
  if (accessBtn) {
    log('🔓 访问验证，点击进入...');
    await accessBtn.click();
    await sleep(2_000);
  }
  await page.waitForSelector('#host', { timeout: 15_000 });
  await sleep(1_000);

  await page.evaluate(
    ({ hostUrl, overseas }) => {
      const cb = document.querySelector<HTMLInputElement>('input[name="line"][value="5"]');
      if (cb) {
        if (overseas && !cb.checked) cb.click();
        else if (!overseas && cb.checked) cb.click();
      }
      const el = document.getElementById('host') as HTMLInputElement;
      el.value = hostUrl;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    },
    { hostUrl, overseas },
  );

  log(`📡 检测节点: itdog（${overseas ? '电信+联通+移动+海外' : '电信+联通+移动'}）`);
  await page.click("button[onclick*=\"check_form('fast')\"]");
  log('⏳ 等待检测完成...');

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const finished = await page.evaluate(() => (window as any)._itdog_finished as boolean);
    const count    = await page.evaluate(() => ((window as any)._itdog_nodes as unknown[]).length);
    if (finished) { log(`✅ itdog 完成，共 ${count} 个节点`); break; }
    if (count)    log(`   已收到 ${count} 个节点，等待完成...`);
    await sleep(5_000);
  }

  return await page.evaluate(() => (window as any)._itdog_nodes as NodeResult[]);
}

async function check17ce(
  page: Page,
  hostUrl: string,
  display: string,
): Promise<NodeResult[]> {
  const log = (msg: string) => console.log(`[${display}] ${msg}`);

  await page.goto('http://17ce.com/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.waitForSelector('#url', { timeout: 15_000 });
  await sleep(500);

  // 只检测大陆节点：取消港澳台(2)和国外(3)，只保留大陆(1)
  await page.evaluate(() => {
    document.querySelectorAll<HTMLInputElement>('input[name="area"]').forEach(cb => {
      cb.checked = cb.value === '1';
    });
    // 同步取消"全部"勾选
    const all = document.querySelector<HTMLInputElement>('input[name="area"][value="0"]');
    if (all) all.checked = false;
  });

  await page.fill('#url', hostUrl);
  log('📡 检测节点: 17ce（大陆）');
  await page.click('#su');
  log('⏳ 等待检测完成...');

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const finished = await page.evaluate(() => (window as any)._17ce_finished as boolean);
    const count    = await page.evaluate(() => ((window as any)._17ce_nodes as unknown[]).length);
    if (finished) { log(`✅ 17ce 完成，共 ${count} 个节点`); break; }
    if (count)    log(`   已收到 ${count} 个节点，等待完成...`);
    await sleep(5_000);
  }

  return await page.evaluate(() => (window as any)._17ce_nodes as NodeResult[]);
}

async function checkChinaz(
  page: Page,
  hostUrl: string,
  display: string,
): Promise<NodeResult[]> {
  const log = (msg: string) => console.log(`[${display}] ${msg}`);

  // 直接导航到结果页（无需手动提交表单）
  const domain = hostUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
  await page.goto(`https://tool.chinaz.com/speedtest/${domain}`, {
    waitUntil: 'domcontentloaded',
    timeout: 30_000,
  });
  log('📡 检测节点: chinaz（国内测速）');
  log('⏳ 等待检测完成...');

  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    await sleep(5_000);
    const finished = await page.evaluate(() => (window as any)._chinaz_finished as boolean);
    const count    = await page.evaluate(() => ((window as any)._chinaz_nodes as unknown[]).length);
    if (finished) { log(`✅ chinaz 完成，共 ${count} 个节点`); break; }
    if (count)    log(`   已收到 ${count} 个节点，等待完成...`);
  }

  return await page.evaluate(() => (window as any)._chinaz_nodes as NodeResult[]);
}

/** 复用已有 page 检测一个域名，根据 platform 调用对应检测函数 */
async function checkWithPage(
  page: Page,
  platform: Platform,
  host: string,
  verbose: boolean,
  threshold: number,
  overseas: boolean,
  display: string,
  shareKey: string | null,
): Promise<CheckResult> {
  const hostUrl = host.includes('://') ? host : `https://${host}`;
  const log = (msg: string) => console.log(`[${display}] ${msg}`);

  log(`\n🔍 检测: ${hostUrl}`);

  let nodes: NodeResult[];
  if (platform === 'itdog') {
    nodes = await checkItdog(page, hostUrl, overseas, display);
  } else if (platform === '17ce') {
    nodes = await check17ce(page, hostUrl, display);
  } else {
    nodes = await checkChinaz(page, hostUrl, display);
  }

  if (verbose && nodes.length) {
    const lines = nodes.map(n => {
      const icon = n.http_code === 200 ? '✅' : '❌';
      return `  ${icon} ${n.name.padEnd(12)} | IP: ${n.ip.padEnd(15)} | HTTP: ${n.http_code}`;
    });
    console.log(`[${display}]\n${lines.join('\n')}`);
  }

  const total   = nodes.length;
  const ok      = nodes.filter(n => n.http_code === 200).length;
  const rate    = total > 0 ? ok / total : 0;
  const blocked = total > 0 ? rate < threshold : null;

  return { host, shareKey, platform, blocked, rate, ok, total, nodes };
}

// ── 并发执行（单浏览器双 Context，itdog + 17ce 各分配 workers） ──────────────
//
// 两个平台各一个 BrowserContext，Cookie 在各自 Context 内共享。
// itdog workers = max(1, floor(N/2))，17ce workers = N - itdog workers。
// 两组 workers 共享同一任务队列，谁先空闲谁接任务。

async function initContextPages(
  browser: Browser,
  platform: Platform,
  count: number,
): Promise<Page[]> {
  const hook = platform === 'itdog' ? ITDOG_HOOK
             : platform === '17ce'  ? CE17_HOOK
             : CHINAZ_HOOK;
  const ua   = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36';

  const ctx = await browser.newContext({ userAgent: ua, viewport: { width: 1920, height: 1080 } });
  await ctx.addInitScript(hook);

  if (platform === 'itdog') {
    // 第一个页面过访问验证，后续复用 Cookie
    const first = await ctx.newPage();
    await first.goto('https://www.itdog.cn/http/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
    const accessBtn = await first.$('#access');
    if (accessBtn) {
      console.log('[itdog] 🔓 通过访问验证...');
      await accessBtn.click();
      await sleep(3_000);
      // 若点击后页面重载未完成，重新导航（Cookie 已设置，不会再触发验证）
      const hostEl = await first.$('#host');
      if (!hostEl) {
        await first.goto('https://www.itdog.cn/http/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
      }
    }
    await first.waitForSelector('#host', { timeout: 30_000 });

    const pages: Page[] = [first];
    for (let i = 1; i < count; i++) {
      const p = await ctx.newPage();
      await p.goto('https://www.itdog.cn/http/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await p.waitForSelector('#host', { timeout: 15_000 });
      pages.push(p);
    }
    return pages;

  } else if (platform === '17ce') {
    // 17ce 无访问验证
    const pages: Page[] = [];
    for (let i = 0; i < count; i++) {
      const p = await ctx.newPage();
      await p.goto('http://17ce.com/', { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await p.waitForSelector('#url', { timeout: 15_000 });
      pages.push(p);
    }
    return pages;

  } else {
    // chinaz：直接导航到首页预热（每次检测时会跳转到结果页）
    const pages: Page[] = [];
    for (let i = 0; i < count; i++) {
      const p = await ctx.newPage();
      await p.goto('https://tool.chinaz.com/speedtest', { waitUntil: 'domcontentloaded', timeout: 30_000 });
      pages.push(p);
    }
    return pages;
  }
}

async function runChecks(
  items: Array<string | Job>,
  verbose: boolean,
  threshold: number,
  overseas: boolean,
  concurrency: number,
  progressEvery = 10,
): Promise<CheckResult[]> {
  const jobs   = normalizeJobs(items);
  const total  = jobs.length;
  if (total === 0) return [];

  const actual      = Math.min(concurrency, MAX_CONCURRENCY, total);
  // 三平台均分：itdog 占 1/3，17ce 占 1/3，chinaz 占剩余
  const itdogCount  = Math.max(1, Math.floor(actual / 3));
  const ce17Count   = Math.floor((actual - itdogCount) / 2);
  const chinazCount = actual - itdogCount - ce17Count;

  if (total > 1) {
    const parts = [
      `itdog×${itdogCount}`,
      ce17Count   > 0 ? `17ce×${ce17Count}`     : '',
      chinazCount > 0 ? `chinaz×${chinazCount}` : '',
    ].filter(Boolean);
    console.log(`📋 共 ${total} 个域名待检测\n⚡ Worker 池（${actual} 并发：${parts.join(' + ')}）\n`);
  }

  let browser: Browser | null = null;
  try {
    browser = await chromium.launch({
      headless: true,
      args: ['--no-proxy-server', '--no-sandbox', '--disable-dev-shm-usage'],
    });

    // 三平台串行初始化；任一失败由其余平台均摊兜底
    let itdogPages:  Page[] = [];
    let ce17Pages:   Page[] = [];
    let chinazPages: Page[] = [];

    try {
      itdogPages = await initContextPages(browser, 'itdog', itdogCount);
    } catch (e) {
      console.warn(`⚠️  [itdog] 初始化失败，将由其他平台兜底：${String(e).split('\n')[0]}`);
    }

    // 计算 17ce 需要承接的额外 worker 数
    const itdogFailed = itdogPages.length === 0 ? itdogCount : 0;
    const ce17Needed  = ce17Count + Math.ceil(itdogFailed / 2);
    if (ce17Needed > 0) {
      if (itdogPages.length > 0) await sleep(2_000);
      try {
        ce17Pages = await initContextPages(browser, '17ce', Math.min(ce17Needed, MAX_CONCURRENCY));
      } catch (e) {
        console.warn(`⚠️  [17ce] 初始化失败：${String(e).split('\n')[0]}`);
      }
    }

    // chinaz 承接剩余 worker
    const ce17Failed    = ce17Pages.length === 0 ? ce17Needed : 0;
    const chinazNeeded  = chinazCount + itdogFailed - Math.ceil(itdogFailed / 2) + ce17Failed;
    if (chinazNeeded > 0) {
      try {
        chinazPages = await initContextPages(browser, 'chinaz', Math.min(chinazNeeded, MAX_CONCURRENCY));
      } catch (e) {
        console.warn(`⚠️  [chinaz] 初始化失败：${String(e).split('\n')[0]}`);
      }
    }

    if (itdogPages.length + ce17Pages.length + chinazPages.length === 0) {
      throw new Error('所有检测平台均初始化失败，请稍后重试');
    }

    const allWorkers: Array<{ page: Page; platform: Platform }> = [
      ...itdogPages.map(p  => ({ page: p, platform: 'itdog'  as Platform })),
      ...ce17Pages.map(p   => ({ page: p, platform: '17ce'   as Platform })),
      ...chinazPages.map(p => ({ page: p, platform: 'chinaz' as Platform })),
    ];
    const desc = [
      itdogPages.length  > 0 ? `itdog×${itdogPages.length}`   : '',
      ce17Pages.length   > 0 ? `17ce×${ce17Pages.length}`     : '',
      chinazPages.length > 0 ? `chinaz×${chinazPages.length}` : '',
    ].filter(Boolean).join(' + ');
    console.log(`✅ ${allWorkers.length} 个 worker 就绪（${desc}）\n`);

    // 共享任务队列
    const queue: Array<[number, Job]> = jobs.map((job, idx) => [idx, job]);
    const results = new Array<CheckResult>(total);
    let completed    = 0;
    let blockedCount = 0;

    await Promise.all(
      allWorkers.map(async ({ page, platform }) => {
        while (queue.length > 0) {
          const item = queue.shift();
          if (!item) break;
          const [idx, [key, d]] = item;
          try {
            results[idx] = await checkWithPage(page, platform, d, verbose, threshold, overseas, jobLabel(key, d), key);
          } catch (e) {
            console.error(`❌ [${jobLabel(key, d)}] 检测异常: ${e}`);
            results[idx] = { host: d, shareKey: key, blocked: null, error: String(e) };
          }
          // 实时写单条结果
          logOneResult(results[idx]);
          if (results[idx].blocked) blockedCount++;
          completed++;
          if (progressEvery && completed % progressEvery === 0) {
            const msg = `📍 进度: 已完成 ${completed}/${total}，发现 ${blockedCount} 个异常`;
            console.log(msg);
            logProgress(completed, total, blockedCount);
          }
        }
      }),
    );

    if (progressEvery && completed % progressEvery !== 0) {
      console.log(`📍 进度: 已完成 ${total}/${total}，发现 ${blockedCount} 个异常`);
    }
    return results;

  } finally {
    try { await browser?.close(); } catch {}
  }
}

async function runChecksBatched(
  items: Array<string | Job>,
  verbose: boolean,
  threshold: number,
  overseas: boolean,
  concurrency: number,
  progressEvery = 10,
  batchSize = 0,
  batchDelay = 30,
): Promise<CheckResult[]> {
  const jobs  = normalizeJobs(items);
  const total = jobs.length;

  if (batchSize <= 0 || total <= batchSize) {
    return runChecks(jobs, verbose, threshold, overseas, concurrency, progressEvery);
  }

  const batches: Job[][] = [];
  for (let i = 0; i < total; i += batchSize) batches.push(jobs.slice(i, i + batchSize));
  console.log(`📦 共 ${total} 个域名，分 ${batches.length} 批（每批最多 ${batchSize} 个）`);

  const allResults: CheckResult[] = [];
  for (let i = 0; i < batches.length; i++) {
    console.log(`\n${'─'.repeat(48)}`);
    console.log(`📦 第 ${i + 1}/${batches.length} 批，共 ${batches[i].length} 个域名`);
    console.log('─'.repeat(48));
    const results = await runChecks(batches[i], verbose, threshold, overseas, concurrency, progressEvery);
    allResults.push(...results);
    if (i < batches.length - 1) {
      console.log(`\n⏸️  批次间隔 ${batchDelay}s，等待中...`);
      await sleep(batchDelay * 1_000);
    }
  }
  return allResults;
}

// ── 输出 & 日志 ────────────────────────────────────────────────────────────

function printSummary(results: CheckResult[]): string[] {
  console.log('\n' + '═'.repeat(56));
  console.log('📊 检测汇总');
  console.log('═'.repeat(56));

  const blockedList: string[] = [];
  for (const r of results) {
    const lab = resultLabel(r);
    const tag = platformTag(r);
    if (r.error) {
      console.log(`  ⚠️  ${tag}${lab} — 检测失败: ${r.error}`);
    } else if (r.blocked === null) {
      console.log(`  ⚠️  ${tag}${lab} — 无节点数据`);
    } else if (r.blocked) {
      console.log(`  🚫 ${tag}${lab} — 疑似被封  (成功率 ${((r.rate ?? 0) * 100).toFixed(1)}%，${r.ok}/${r.total} 节点)`);
      blockedList.push(lab);
    } else {
      console.log(`  ✅ ${tag}${lab} — 正常访问  (成功率 ${((r.rate ?? 0) * 100).toFixed(1)}%，${r.ok}/${r.total} 节点)`);
    }
  }

  console.log('═'.repeat(56));
  if (blockedList.length) {
    console.log(`🚨 发现 ${blockedList.length} 个疑似被封域名:`);
    blockedList.forEach(d => console.log(`   - ${d}`));
  } else {
    console.log('🎉 所有域名均可正常访问');
  }
  return blockedList;
}

function platformTag(r: CheckResult): string {
  return r.platform ? `[${r.platform}] ` : '';
}

function resultLogLine(r: CheckResult): string {
  const lab = resultLabel(r);
  const tag = platformTag(r);
  if (r.error)               return `  ERROR   ${tag}${lab}: ${r.error}`;
  if (r.blocked === null)    return `  NO_DATA ${tag}${lab}`;
  if (r.blocked)             return `  BLOCKED ${tag}${lab}  (${((r.rate ?? 0) * 100).toFixed(1)}% ${r.ok}/${r.total})`;
  return                            `  OK      ${tag}${lab}  (${((r.rate ?? 0) * 100).toFixed(1)}% ${r.ok}/${r.total})`;
}

function logOneResult(r: CheckResult) {
  ensureDir();
  fs.appendFileSync(LOG_PATH, resultLogLine(r) + '\n', 'utf-8');
}

function logProgress(completed: number, total: number, blockedCount: number) {
  ensureDir();
  const line = `  [进度] ${fmtNow()}  已完成 ${completed}/${total}，发现 ${blockedCount} 个异常`;
  fs.appendFileSync(LOG_PATH, line + '\n', 'utf-8');
}

function logSessionStart(total: number) {
  ensureDir();
  const sep = '='.repeat(56);
  fs.appendFileSync(LOG_PATH, `\n${sep}\n开始 ${fmtNow()}，共 ${total} 个域名\n${sep}\n`, 'utf-8');
}

function logSessionEnd(total: number, blockedCount: number) {
  ensureDir();
  const sep = '='.repeat(56);
  fs.appendFileSync(LOG_PATH, `${sep}\n完成 ${fmtNow()}，共 ${total} 个，异常 ${blockedCount} 个\n${sep}\n`, 'utf-8');
}

// ── CLI 参数解析 ─────────────────────────────────────────────────────────────

/** 读取带值的 flag，如 --threshold 0.7 / -c 3 */
function flagValue(args: string[], ...flags: string[]): string | undefined {
  for (const f of flags) {
    const i = args.indexOf(f);
    if (i !== -1 && i + 1 < args.length) return args[i + 1];
  }
}

function flagBool(args: string[], ...flags: string[]): boolean {
  return flags.some(f => args.includes(f));
}

function intOr(s: string | undefined, def: number): number {
  const n = parseInt(s ?? '', 10); return isNaN(n) ? def : n;
}

function floatOr(s: string | undefined, def: number): number {
  const n = parseFloat(s ?? ''); return isNaN(n) ? def : n;
}

/** 从 args 中提取所有非 flag 的位置参数（跳过带值 flag 的参数值） */
function posArgs(args: string[], valuedFlags: string[]): string[] {
  const valued = new Set(valuedFlags);
  const result: string[] = [];
  let i = 0;
  while (i < args.length) {
    if (valued.has(args[i])) { i += 2; continue; }
    if (!args[i].startsWith('-')) result.push(args[i]);
    i++;
  }
  return result;
}

// ── 主入口 ───────────────────────────────────────────────────────────────────

async function main() {
  const rawArgs = process.argv.slice(2);
  const subcmd  = rawArgs[0];

  // ── list ──
  if (subcmd === 'list') { cmdList(); return; }

  // ── add ──
  if (subcmd === 'add') {
    const file    = flagValue(rawArgs, '--file', '-f');
    const domains = posArgs(rawArgs.slice(1), ['--file', '-f']);
    if (file) domains.push(...readDomainsFromFile(file));
    if (!domains.length) { console.log('用法: check_domain.ts add <域名1> [域名2] ... [-f 文件]'); process.exit(1); }
    cmdAdd(domains);
    return;
  }

  // ── remove ──
  if (subcmd === 'remove') {
    const file    = flagValue(rawArgs, '--file', '-f');
    const domains = posArgs(rawArgs.slice(1), ['--file', '-f']);
    if (file) domains.push(...readDomainsFromFile(file));
    if (!domains.length) { console.log('用法: check_domain.ts remove <域名1> [域名2] ... [-f 文件]'); process.exit(1); }
    cmdRemove(domains);
    return;
  }

  // ── sync ──
  if (subcmd === 'sync') {
    let url = rawArgs[1] && !rawArgs[1].startsWith('-') ? rawArgs[1] : undefined;
    if (!url) {
      const meta = loadSyncMeta();
      if (meta.url) { url = meta.url; console.log(`ℹ️  使用上次同步地址: ${url}`); }
      else { console.log('用法: check_domain.ts sync <url> [--batch-size N] [--force]'); process.exit(1); }
    }
    const force     = flagBool(rawArgs, '--force');
    const batchSize = intOr(flagValue(rawArgs, '--batch-size', '-B'), 0);
    await cmdSync(url, force, batchSize);
    return;
  }

  // ── run ──
  if (subcmd === 'run') {
    const verbose     = flagBool(rawArgs, '--verbose', '-v');
    const overseas    = flagBool(rawArgs, '--overseas', '-o');
    const synced      = flagBool(rawArgs, '--synced', '-s');
    const file        = flagValue(rawArgs, '--file', '-f');
    const threshold   = floatOr(flagValue(rawArgs, '--threshold'), DEFAULT_THRESHOLD);
    const concurrency = intOr(flagValue(rawArgs, '--concurrency', '-c'), DEFAULT_CONCURRENCY);
    const progEvery   = intOr(flagValue(rawArgs, '--progress-every'), 10);
    let   batchSize   = intOr(flagValue(rawArgs, '--batch-size', '-B'), 0);
    const batchDelay  = floatOr(flagValue(rawArgs, '--batch-delay'), 30);

    let items: Job[] = [];
    if (synced) {
      items = loadSyncedJobs();
      if (!items.length) {
        console.error(`❌ 未找到同步数据或列表为空: ${SYNCED_MAP_PATH}`);
        console.error('   请先执行: check_domain.ts sync <url>   或   sync --force');
        process.exit(1);
      }
      if (batchSize === 0) {
        const metaBs = loadSyncMeta().batch_size ?? 0;
        if (metaBs > 0) { batchSize = metaBs; console.log(`ℹ️  使用 sync 时记录的分批大小: ${metaBs} 个/批`); }
      }
    } else if (file) {
      items = readJobsFromFile(file);
    } else {
      items = loadWatchlist().map(d => [null, d]);
    }

    if (!items.length) {
      console.error('❌ 监控列表为空，请先用 `add <域名>` 添加域名，或用 -f 指定文件');
      process.exit(1);
    }

    console.log(`🕐 开始: ${fmtNow()}`);
    logSessionStart(items.length);
    const results = await runChecksBatched(
      items, verbose, threshold, overseas, concurrency, progEvery, batchSize, batchDelay,
    );
    const blocked = printSummary(results);
    logSessionEnd(results.length, blocked.length);
    console.log(`\n🕐 完成: ${fmtNow()}`);
    process.exit(blocked.length > 0 ? 1 : 0);
  }

  // ── 一次性检测 ──
  const valuedFlagNames = ['--threshold', '--concurrency', '-c', '--progress-every', '--file', '-f'];
  const file        = flagValue(rawArgs, '--file', '-f');
  const verbose     = flagBool(rawArgs, '--verbose', '-v');
  const overseas    = flagBool(rawArgs, '--overseas', '-o');
  const threshold   = floatOr(flagValue(rawArgs, '--threshold'), DEFAULT_THRESHOLD);
  const concurrency = intOr(flagValue(rawArgs, '--concurrency', '-c'), DEFAULT_CONCURRENCY);
  const progEvery   = intOr(flagValue(rawArgs, '--progress-every'), 10);

  const items: Job[] = posArgs(rawArgs, valuedFlagNames).map(d => [null, normalizeDomain(d)]);
  if (file) items.push(...readJobsFromFile(file));

  if (!items.length) {
    console.log(`域名封锁检测（via itdog.cn + Playwright）

用法:
  npx ts-node check_domain.ts <域名...>         一次性检测
  npx ts-node check_domain.ts add <域名...>     添加到监控列表
  npx ts-node check_domain.ts remove <域名...>  从监控列表删除
  npx ts-node check_domain.ts list              查看监控列表
  npx ts-node check_domain.ts sync <url>        从远程 JSON 同步域名
  npx ts-node check_domain.ts run               检测监控列表

选项:
  -v, --verbose           显示各节点详情
  -o, --overseas          包含港澳台、海外节点
  --threshold <N>         封锁判定阈值（默认 0.7）
  -c, --concurrency <N>   并发数（默认 3，最大 5）
  -f, --file <文件>       从文件读取域名
  --progress-every <N>    每完成 N 个打印进度（默认 10）`);
    process.exit(1);
  }

  console.log(`🕐 开始: ${fmtNow()}`);
  logSessionStart(items.length);
  const results = await runChecks(items, verbose, threshold, overseas, concurrency, progEvery);
  const blocked = printSummary(results);
  logSessionEnd(results.length, blocked.length);
  console.log(`\n🕐 完成: ${fmtNow()}`);
  process.exit(blocked.length > 0 ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });