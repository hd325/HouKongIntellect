// Compile with the checked-in Tailwind v3 browser bundle, entirely offline.
// NODE_PATH=<bundled node_modules> node scripts/build_tailwind_css.cjs [--verify] [--apply]
// --apply replaces runtime compilation with the generated inline CSS after verification.
// This command never contacts the app or runs requests against business data.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const CSS_PATH = path.join(ROOT, 'tailwind.generated.css');
const ORIGIN = 'http://tailwind-fixture.test';
const RUNTIME_TAG = /\s*<script\s+defer\s+src=["']\/static\/tailwindcss\.js["']><\/script>/;
const GENERATED_BLOCK = /\s*<!-- BEGIN GENERATED TAILWIND -->[\s\S]*?<!-- END GENERATED TAILWIND -->/g;
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const channel = process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined);

function sourceCandidates(html) {
  // Scan the entire source, including JS template strings, classList calls,
  // constants and markdown renderers. Extra non-utility tokens are ignored by
  // this exact compiler. Arbitrary values/variants keep their brackets intact.
  const candidates = new Set();
  for (const match of html.matchAll(/[^\s"'`<>=;{}]+/g)) {
    const token = match[0];
    if (token.length > 180 || !/[a-z]/i.test(token)) continue;
    candidates.add(token);
    const trimmed = token.replace(/^[(),]+|[(),]+$/g, '');
    if (trimmed) candidates.add(trimmed);
  }
  return [...candidates].sort();
}

async function generate(browser, source, compiler) {
  const page = await browser.newPage();
  try {
    // No source app script runs here. Tokens are inserted as DOM data, not HTML.
    await page.setContent('<!doctype html><html><head></head><body><div id="candidates"></div></body></html>');
    await page.evaluate(tokens => {
      document.getElementById('candidates').setAttribute('class', tokens.join(' '));
    }, sourceCandidates(source));
    await page.addScriptTag({ content: compiler });
    // The runtime observes mutations, so explicitly trigger one after startup.
    await page.evaluate(() => document.body.classList.add('antialiased'));
    await page.waitForFunction(() => [...document.head.querySelectorAll('style')]
      .some(style => style.textContent.includes('--tw-border-spacing-x') && style.textContent.includes('.hidden')));
    return await page.evaluate(() => [...document.head.querySelectorAll('style')]
      .find(style => style.textContent.includes('--tw-border-spacing-x')).textContent);
  } finally { await page.close(); }
}

function staticHtml(source, css) {
  assert.ok(!css.includes('</style'), 'Generated CSS cannot contain an HTML closing style tag');
  return source.replace(RUNTIME_TAG, '').replace('</head>',
    `    <!-- BEGIN GENERATED TAILWIND -->\n    <style id="tailwind-generated">${css}</style>\n    <!-- END GENERATED TAILWIND -->\n</head>`);
}

const USER = { account: 'tailwind-fixture', name: '樣式測試', class: '高二測試班', role: 'student' };
const HISTORY = [{ id: 'style-history', title: '化學學習對話', message_count: 2, updated_at: '2026-10-18T08:00:00Z' }];
const FILES = [
  { id: 'style-pdf', name: '化學筆記.pdf', subject: 'chemistry', size: 128, audience: 'personal', owner: USER.account, uploaded_at: '2026-10-18T08:00:00Z' },
  { id: 'style-docx', name: '數學練習.docx', subject: 'math', size: 256, audience: 'school', owner: USER.account, uploaded_at: '2026-10-18T08:00:00Z' },
];
const CONVERSATION = { id: HISTORY[0].id, messages: [
  { content: '緩存中的化學問題', is_user: true, timestamp: '2026-10-18T08:00:00Z' },
  { content: '合成測試回答', is_user: false, timestamp: '2026-10-18T08:00:01Z' },
] };

async function fixture(browser, html, viewport, theme = 'light') {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  const apiRequests = [];
  let release;
  const apiHeld = new Promise(resolve => { release = resolve; });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(({ user, history, files, conversation, theme }) => {
    const session = 'tailwind-fixture-session';
    localStorage.setItem('sessionId', session);
    localStorage.setItem('userInfo', JSON.stringify(user));
    localStorage.setItem('theme', theme);
    sessionStorage.setItem('houkong.startup.v1:' + session, JSON.stringify({
      version: 1, savedAt: Date.now(), userInfo: user, history, files, currentConversation: conversation,
    }));
    window.__styleFixtureFrames = { app: null, history: null, files: null };
    const visible = element => element && getComputedStyle(element).display !== 'none' && element.getClientRects().length > 0;
    function frame() {
      const now = performance.now();
      const frames = window.__styleFixtureFrames;
      if (visible(document.getElementById('app'))) {
        if (frames.app === null) frames.app = now;
        if (frames.history === null && document.querySelector('#history-list .history-item')) frames.history = now;
        if (frames.files === null && document.querySelector('#all-files-list .file-item')) frames.files = now;
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }, { user: USER, history: HISTORY, files: FILES, conversation: CONVERSATION, theme });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.fulfill({ contentType: 'text/css', body: '' });
    if (url.pathname.startsWith('/api/')) {
      apiRequests.push(url.pathname);
      await apiHeld;
      return route.fulfill({ json: { code: 200, data: {} } });
    }
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
    const file = path.resolve(ROOT, decodeURIComponent(url.pathname.replace(/^\/static\//, '')));
    if (!file.startsWith(ROOT + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404, body: '' });
    const type = { '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf' }[path.extname(file)] || 'application/octet-stream';
    return route.fulfill({ contentType: type, body: fs.readFileSync(file) });
  });
  await page.goto(ORIGIN, { waitUntil: 'commit' });
  await page.waitForFunction(() => window.__styleFixtureFrames?.files !== null
    && document.querySelector('#all-files-list .file-item'));
  await page.waitForLoadState('domcontentloaded');
  // A hidden class must work before comparing geometry. Runtime compilation is async.
  await page.waitForFunction(() => getComputedStyle(document.getElementById('login-page')).display === 'none');
  await page.evaluate(theme => {
    document.documentElement.dataset.theme = theme;
    document.getElementById('file-panel')?.classList.remove('collapsed');
    document.getElementById('sidebar')?.classList.remove('hidden');
  }, theme);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.evaluate(() => Promise.all(document.getAnimations()
    .filter(animation => animation.effect?.getTiming().iterations !== Infinity)
    .map(animation => animation.finished.catch(() => {}))));
  return { page, errors, apiRequests, async close() { release(); await context.close(); } };
}

async function styles(page) {
  return page.evaluate(() => {
    const properties = ['display', 'visibility', 'position', 'width', 'height', 'minWidth', 'maxWidth',
      'color', 'backgroundColor', 'borderTopWidth', 'borderTopColor', 'borderRadius', 'boxShadow',
      'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'textAlign', 'flexDirection', 'flexGrow',
      'alignItems', 'justifyContent', 'gap', 'overflow', 'marginTop', 'marginRight', 'marginBottom',
      'marginLeft', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'transform'];
    return [...document.body.querySelectorAll('*')]
      .filter(element => !['SCRIPT', 'STYLE'].includes(element.tagName))
      .map((element, index) => {
        const computed = getComputedStyle(element);
        return { index, tag: element.tagName, id: element.id, class: element.getAttribute('class'),
          ...Object.fromEntries(properties.map(property => [property, computed[property]])) };
      });
  });
}

async function verify(browser, source, css) {
  const runtimeSource = RUNTIME_TAG.test(source) ? source
    : source.replace('</head>', '<script defer src="/static/tailwindcss.js"></script>\n</head>');
  const results = [];
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    for (const theme of ['light', 'dark']) {
      const before = await fixture(browser, runtimeSource, viewport, theme);
      const after = await fixture(browser, staticHtml(source, css), viewport, theme);
      try {
        const original = await styles(before.page);
        const generated = await styles(after.page);
        const differences = [];
        for (let i = 0; i < Math.max(original.length, generated.length); i++) {
          const left = original[i], right = generated[i];
          if (JSON.stringify(left) !== JSON.stringify(right)) differences.push({ original: left, generated: right });
        }
        assert.equal(before.errors.length, 0, before.errors.join('\n'));
        assert.equal(after.errors.length, 0, after.errors.join('\n'));
        assert.equal(differences.length, 0, JSON.stringify(differences.slice(0, 3), null, 2));
        results.push({ viewport, theme, elementsCompared: original.length, exactComputedStyleParity: true,
          runtimeFrames: await before.page.evaluate(() => window.__styleFixtureFrames),
          staticFrames: await after.page.evaluate(() => window.__styleFixtureFrames),
          runtimePaints: await before.page.evaluate(() => performance.getEntriesByType('paint').map(item => ({ name: item.name, ms: item.startTime }))),
          staticPaints: await after.page.evaluate(() => performance.getEntriesByType('paint').map(item => ({ name: item.name, ms: item.startTime }))) });
      } finally { await before.close(); await after.close(); }
    }
  }
  return results;
}

(async () => {
  const originalHtml = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  const source = originalHtml.replace(GENERATED_BLOCK, '');
  const compiler = fs.readFileSync(path.join(ROOT, 'tailwindcss.js'), 'utf8');
  const browser = await chromium.launch({ headless: true, channel });
  try {
    const css = await generate(browser, source, compiler);
    const banner = `/* Generated by scripts/build_tailwind_css.cjs with the checked-in Tailwind v3 runtime.\n * Compiler SHA-256: ${hash(compiler)}\n * Rebuild after adding utility classes to index.html; do not edit manually. */\n`;
    const embedded = originalHtml.match(/<style id="tailwind-generated">([\s\S]*?)<\/style>/);
    if (process.argv.includes('--verify') && !process.argv.includes('--apply') && embedded) {
      assert.equal(embedded[1], banner + css, 'Inline utility CSS is stale; rebuild with --apply');
    }
    fs.writeFileSync(CSS_PATH, banner + css + '\n');
    const result = { css: path.relative(ROOT, CSS_PATH), bytes: Buffer.byteLength(banner + css + '\n'),
      candidateCount: sourceCandidates(source).length, compilerSha256: hash(compiler),
      cssSha256: hash(css), sourceSha256: hash(source), indexEdited: false };
    if (process.argv.includes('--verify') || process.argv.includes('--apply')) result.parity = await verify(browser, source, css);
    if (process.argv.includes('--apply')) {
      fs.writeFileSync(path.join(ROOT, 'index.html'), staticHtml(source, banner + css));
      result.indexEdited = true;
    }
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
