// Offline browser regression: production HTML/assets, synthetic account and API only.
// Install Playwright, then run: node tests/startup_ui_check.cjs
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const ORIGIN = 'http://127.0.0.1:8000';
const SESSION = 'startup-fixture-session';
const CACHE_KEY = 'houkong.startup.v1:' + SESSION;
const USER = { account: 'startup-fixture-account', name: '啟動測試', class: '高二測試班', role: 'student' };
const HISTORY = [{ id: 'fixture-conversation', title: '緩存對話標題', message_count: 2, updated_at: '2026-10-18T08:00:00Z' }];
const FILES = [
  { id: 'fixture-chemistry', name: '緩存化學.pdf', subject: 'chemistry', size: 128, audience: 'personal', owner: USER.account, uploaded_at: '2026-10-18T08:00:00Z' },
  { id: 'fixture-math', name: '緩存數學.docx', subject: 'math', size: 256, audience: 'personal', owner: USER.account, uploaded_at: '2026-10-18T08:00:00Z' },
];
const CONVERSATION = { id: HISTORY[0].id, messages: [
  { content: '緩存中的問題', is_user: true, timestamp: '2026-10-18T08:00:00Z' },
  { content: '**緩存中的回答**', is_user: false, timestamp: '2026-10-18T08:00:01Z' },
] };
const STARTUP_ENDPOINTS = ['/api/session', '/api/files', '/api/conversations', '/api/skills'];
const results = [];
const caseFilter = process.env.STARTUP_CASE ? new RegExp(process.env.STARTUP_CASE, 'i') : null;

function snapshot(overrides = {}) {
  return { version: 1, savedAt: Date.now(), userInfo: USER, history: HISTORY, files: FILES, currentConversation: CONVERSATION, ...overrides };
}

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
}

async function fixture(browser, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  const assetRequests = [];
  let held = gate();
  let staticHeld = gate();
  let responseMode = options.responseMode || 'success';
  let networkHistory = HISTORY;
  let networkFiles = FILES;
  let networkConversation = CONVERSATION;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(({ session, user, key, cache, cacheKey, storageBlocked }) => {
    if (!localStorage.getItem('__startupFixtureSeeded')) {
      localStorage.setItem('__startupFixtureSeeded', '1');
      localStorage.setItem('sessionId', session);
      localStorage.setItem('userInfo', JSON.stringify(user));
      if (cache !== null) sessionStorage.setItem(cacheKey || key, typeof cache === 'string' ? cache : JSON.stringify(cache));
    }
    if (storageBlocked) Object.defineProperty(window, 'sessionStorage', { configurable: true, get() { throw new DOMException('Synthetic storage denial', 'SecurityError'); } });
    window.__startupFrames = { app: null, history: null, files: null, messages: null };
    const visible = element => element && !element.closest('.hidden') && getComputedStyle(element).display !== 'none' && element.getClientRects().length > 0;
    function measure() {
      const now = performance.now();
      const app = document.getElementById('app');
      const frames = window.__startupFrames;
      if (visible(app)) {
        if (frames.app === null) frames.app = now;
        if (frames.history === null && document.querySelector('#history-list .history-item')) frames.history = now;
        if (frames.files === null && document.querySelector('#all-files-list .file-item')) frames.files = now;
        if (frames.messages === null && document.querySelector('#messages-container .message-row')) frames.messages = now;
      }
      requestAnimationFrame(measure);
    }
    requestAnimationFrame(measure);
  }, {
    session: SESSION, user: USER, key: CACHE_KEY,
    cache: Object.hasOwn(options, 'cache') ? options.cache : snapshot(),
    cacheKey: options.cacheKey, storageBlocked: !!options.storageBlocked,
  });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) {
      // Font CDNs and any other remote resource stay entirely offline.
      return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
    }
    if (url.pathname.startsWith('/api/')) {
      const endpoint = url.pathname;
      requests.push(endpoint);
      if (endpoint === '/api/new-conversation') return route.fulfill({ json: { code: 200, data: { conversation_id: 'fixture-new-conversation' } } });
      if (endpoint === '/api/conversation/fixture-other-conversation/switch') return route.fulfill({ json: { code: 200, data: { messages: [{ content: '使用者切換後的內容', is_user: true, timestamp: '2026-10-18T08:00:00Z' }] } } });
      if (options.mockChat && endpoint === '/api/chat') {
        const question = route.request().postDataJSON().question;
        const answer = '合成化學老師回答：觀察碳碳雙鍵，判斷加成反應。';
        const skill = { skill_id: 'chemistry-teacher', label: '化學老師', status: 'no_relevant_sources', style_verified: true, sources: [] };
        networkConversation = { id: CONVERSATION.id, messages: [...networkConversation.messages,
          { content: question, is_user: true, timestamp: '2026-10-18T08:01:00Z' },
          { content: answer, is_user: false, timestamp: '2026-10-18T08:01:01Z', skill_id: 'chemistry-teacher', skill_context: skill, citation_map: [] },
        ] };
        const events = [{ type: 'skill_context', ...skill },
          { type: 'no_relevant_context', status: 'no_relevant_context', chunks: [] },
          { type: 'content', text: answer },
          { type: 'done', skill_id: 'chemistry-teacher', skill_context: skill, citation_map: [] }];
        return route.fulfill({ contentType: 'text/event-stream; charset=utf-8', body: events.map(event => 'data: ' + JSON.stringify(event) + '\n\n').join('') });
      }
      await held.promise;
      if (responseMode === 'offline' && endpoint === '/api/session') return route.abort('failed');
      if (responseMode === 'errors' && ['/api/files', '/api/conversations'].includes(endpoint)) {
        return route.fulfill({ status: 503, json: { code: 503, message: 'Synthetic background failure' } });
      }
      if (responseMode === 'unauthorized' && endpoint === '/api/session') {
        return route.fulfill({ status: 401, json: { code: 401, message: 'Synthetic expired session' } });
      }
      const payload = endpoint === '/api/session'
        ? { code: 200, data: { user_info: USER, current_conversation: networkConversation } }
        : endpoint === '/api/files' ? { code: 200, data: { files: networkFiles } }
        : endpoint === '/api/conversations' ? { code: 200, data: { conversations: networkHistory } }
        : endpoint === '/api/skills' ? { code: 200, data: { skills: [] } }
        : { code: 404, message: 'Unknown synthetic endpoint' };
      return route.fulfill({ status: payload.code === 404 ? 404 : 200, json: payload });
    }
    if (url.pathname === '/' || url.pathname === '/index.html') {
      return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(ROOT, 'index.html')) });
    }
    assetRequests.push(url.pathname);
    if (options.mockPdf && url.pathname === '/static/pdf.min.js') {
      return route.fulfill({ contentType: 'application/javascript', body: 'window.pdfjsLib = { GlobalWorkerOptions: {} };' });
    }
    if (['/static/marked.js', '/static/pdf.min.js'].includes(url.pathname)) await staticHeld.promise;
    const asset = path.resolve(ROOT, decodeURIComponent(url.pathname.replace(/^\/static\//, '')));
    if (!asset.startsWith(ROOT + path.sep) || !fs.existsSync(asset) || !fs.statSync(asset).isFile()) return route.fulfill({ status: 404, body: '' });
    const extension = path.extname(asset);
    const contentType = { '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' }[extension] || 'application/octet-stream';
    return route.fulfill({ contentType, body: fs.readFileSync(asset) });
  });
  return {
    page, errors, requests, assetRequests,
    count(endpoint) { return requests.filter(item => item === endpoint).length; },
    release() { held.release(); staticHeld.release(); },
    releaseApi() { held.release(); },
    releaseStatic() { staticHeld.release(); },
    reset(options = {}) {
      held.release(); staticHeld.release();
      held = gate(); staticHeld = gate(); requests.length = 0;
      responseMode = options.responseMode || 'success';
    },
    setData({ history = networkHistory, files = networkFiles, conversation = networkConversation }) {
      networkHistory = history; networkFiles = files; networkConversation = conversation;
    },
    async open() { await page.goto(ORIGIN, { waitUntil: 'commit' }); },
    async close() { held.release(); staticHeld.release(); await context.close(); },
  };
}

async function shellIsImmediate(test, label) {
  await test.page.waitForFunction(() => Number.isFinite(window.__startupFrames?.app), null, { timeout: 1800 });
  const timing = await test.page.evaluate(() => window.__startupFrames.app);
  assert.ok(timing < 1000, `${label}: app first frame ${timing.toFixed(1)}ms exceeds 1 second`);
  assert.equal(await test.page.locator('#login-page').isVisible(), false, label + ': login must not cover saved session');
  assert.equal(await test.page.locator('#ai-text-input').isEnabled(), true);
  return timing;
}

async function cachedContentIsImmediate(test, label) {
  const appMs = await shellIsImmediate(test, label);
  await test.page.waitForFunction(() => {
    const frames = window.__startupFrames;
    return frames && [frames.history, frames.files, frames.messages].every(Number.isFinite);
  }, null, { timeout: 1000 });
  const frames = await test.page.evaluate(() => window.__startupFrames);
  for (const [name, time] of Object.entries(frames)) assert.ok(time < 1000, `${label}: ${name} first frame ${time}ms`);
  assert.match(await test.page.locator('#history-list').textContent(), /緩存對話標題/);
  assert.match(await test.page.locator('#all-files-list').textContent(), /緩存化學.pdf/);
  assert.match(await test.page.locator('#messages-container').textContent(), /緩存中的問題/);
  assert.match(await test.page.locator('#messages-container').textContent(), /緩存中的回答/);
  assert.doesNotMatch(await test.page.locator('#history-list').textContent(), /加載中|載入中|加载中/);
  return { appMs, ...frames };
}

async function oncePerEndpoint(test) {
  await test.page.waitForFunction(() => performance.now() > 200);
  for (const endpoint of STARTUP_ENDPOINTS) assert.equal(test.count(endpoint), 1, `${endpoint}: startup must request exactly once`);
}

async function runCase(browser, name, options, check) {
  if (caseFilter && !caseFilter.test(name)) return;
  const test = await fixture(browser, options);
  try {
    await test.open();
    const details = await check(test);
    assert.deepEqual(test.errors, [], name + ': uncaught browser errors');
    results.push({ name, passed: true, ...details });
  } finally { await test.close(); }
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined) });
  try {
    await runCase(browser, 'cached refresh before APIs or Markdown/PDF arrive', {}, async test => {
      const firstFrame = await cachedContentIsImmediate(test, 'warm startup');
      await oncePerEndpoint(test);
      const beforeFilter = test.count('/api/files');
      await test.page.locator('#filter-subject').selectOption('chemistry');
      assert.equal(await test.page.locator('#all-files-list .file-item').count(), 1);
      assert.match(await test.page.locator('#all-files-list').textContent(), /緩存化學.pdf/);
      await test.page.locator('#filter-type').selectOption('docx');
      assert.equal(await test.page.locator('#all-files-list .file-item').count(), 0);
      await test.page.locator('#filter-subject').selectOption('');
      assert.match(await test.page.locator('#all-files-list').textContent(), /緩存數學.docx/);
      await test.page.locator('#filter-type').selectOption('');
      assert.equal(test.count('/api/files'), beforeFilter, 'local filters must not make network requests');
      const refreshedFiles = FILES.map(file => ({ ...file, name: file.name.replace('緩存', '後台更新') }));
      const refreshedHistory = HISTORY.map(item => ({ ...item, title: '後台更新對話標題' }));
      test.setData({ files: refreshedFiles, history: refreshedHistory });
      test.release();
      await test.page.locator('#all-files-list .file-name').filter({ hasText: '後台更新化學.pdf' }).waitFor();
      await test.page.locator('#history-list .conversation-title').filter({ hasText: '後台更新對話標題' }).waitFor();
      await test.page.locator('.message-row.ai .message-bubble strong').filter({ hasText: '緩存中的回答' }).waitFor();
      await oncePerEndpoint(test);
      const saved = await test.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), CACHE_KEY);
      assert.equal(saved.files[0].name, '後台更新化學.pdf');
      assert.equal(saved.history[0].title, '後台更新對話標題');
      test.reset();
      await test.page.reload({ waitUntil: 'commit' });
      const refreshMs = await shellIsImmediate(test, 'actual refresh');
      assert.match(await test.page.locator('#history-list').textContent(), /後台更新對話標題/);
      assert.match(await test.page.locator('#all-files-list').textContent(), /後台更新化學.pdf/);
      assert.match(await test.page.locator('#messages-container').textContent(), /緩存中的回答/);
      await oncePerEndpoint(test);
      const animations = await test.page.locator('.message-row').evaluateAll(rows => rows.map(row => ({ animation: getComputedStyle(row).animationName, opacity: getComputedStyle(row).opacity })));
      assert.ok(animations.every(item => item.animation === 'none' && item.opacity === '1'), 'restored messages must be fully visible without entry animation');
      await test.page.evaluate(() => addMessage('立即顯示的新問題', true));
      assert.deepEqual(await test.page.locator('.message-row').last().evaluate(row => ({ animation: getComputedStyle(row).animationName, opacity: getComputedStyle(row).opacity })), { animation: 'none', opacity: '1' });
      await test.page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await test.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, '390px viewport must not overflow horizontally');
      return { firstFrame, actualRefreshMs: refreshMs, localFilters: true, mobileNoOverflow: true };
    });

    await runCase(browser, 'background errors keep cached lists', { responseMode: 'errors' }, async test => {
      const firstFrame = await cachedContentIsImmediate(test, 'background errors');
      test.release();
      await test.page.waitForLoadState('load');
      await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
      assert.match(await test.page.locator('#history-list').textContent(), /緩存對話標題/);
      assert.match(await test.page.locator('#all-files-list').textContent(), /緩存化學.pdf/);
      return { firstFrame };
    });

    await runCase(browser, 'offline session keeps cached shell', { responseMode: 'offline' }, async test => {
      const firstFrame = await cachedContentIsImmediate(test, 'offline session');
      const failedSession = test.page.waitForEvent('requestfailed', request => new URL(request.url()).pathname === '/api/session');
      test.release();
      await failedSession;
      await test.page.waitForLoadState('load');
      await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await test.page.locator('#app').isVisible(), true);
      assert.equal(await test.page.evaluate(() => localStorage.getItem('sessionId')), SESSION);
      assert.match(await test.page.locator('#history-list').textContent(), /緩存對話標題/);
      return { firstFrame };
    });

    await runCase(browser, '401 clears identity and cached data', { responseMode: 'unauthorized' }, async test => {
      await cachedContentIsImmediate(test, 'unauthorized session');
      test.releaseApi();
      await test.page.locator('#login-page').waitFor({ state: 'visible' });
      assert.equal(await test.page.locator('#app').isVisible(), false);
      assert.deepEqual(await test.page.evaluate(key => ({ session: localStorage.getItem('sessionId'), user: localStorage.getItem('userInfo'), cache: sessionStorage.getItem(key) }), CACHE_KEY), { session: null, user: null, cache: null });
      return {};
    });

    await runCase(browser, 'valid empty lists render without a loading state', { cache: snapshot({ history: [], files: [], currentConversation: null }), responseMode: 'errors' }, async test => {
      const appMs = await shellIsImmediate(test, 'empty cache');
      assert.match(await test.page.locator('#history-list').textContent(), /暫無歷史對話/);
      assert.match(await test.page.locator('#all-files-list').textContent(), /暫無上傳檔案/);
      assert.doesNotMatch(await test.page.locator('#history-list').textContent(), /加載中|載入中|加载中/);
      test.release();
      await test.page.waitForLoadState('load');
      await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
      assert.match(await test.page.locator('#history-list').textContent(), /暫無歷史對話/);
      return { appMs };
    });

    await runCase(browser, 'late session preserves a message entered after first frame', {}, async test => {
      await cachedContentIsImmediate(test, 'late session and message');
      await test.page.evaluate(() => addMessage('使用者剛輸入的新訊息', true));
      test.releaseApi();
      await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
      await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.match(await test.page.locator('#messages-container').textContent(), /使用者剛輸入的新訊息/);
      const saved = await test.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), CACHE_KEY);
      assert.equal(saved.currentConversation.messages.at(-1).content, '使用者剛輸入的新訊息');
      return {};
    });

    for (const action of ['new', 'switch']) {
      await runCase(browser, `late session preserves ${action} conversation`, {}, async test => {
        await cachedContentIsImmediate(test, 'late session and conversation action');
        if (action === 'new') {
          await test.page.locator('#new-chat-btn').click();
          await test.page.waitForFunction(() => currentConversationId === 'fixture-new-conversation');
        } else {
          await test.page.evaluate(() => switchToConversation('fixture-other-conversation'));
        }
        test.releaseApi();
        await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
        await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await test.page.evaluate(() => currentConversationId), action === 'new' ? 'fixture-new-conversation' : 'fixture-other-conversation');
        if (action === 'new') assert.equal(await test.page.locator('#messages-container .message-row').count(), 0);
        else assert.match(await test.page.locator('#messages-container').textContent(), /使用者切換後的內容/);
        return {};
      });
    }

    await runCase(browser, 'lazy PDF concurrent callers share one script request', { mockPdf: true }, async test => {
      await shellIsImmediate(test, 'lazy PDF');
      assert.equal(test.assetRequests.filter(url => url === '/static/pdf.min.js').length, 0, 'startup must not request PDF.js');
      await test.page.evaluate(() => {
        const first = ensurePdfLibrary();
        const second = ensurePdfLibrary();
        window.__pdfSharedPromise = first === second;
        window.__pdfReady = false;
        Promise.all([first, second]).then(([one, two]) => { window.__pdfSameLibrary = one === two; window.__pdfReady = true; });
      });
      await test.page.waitForFunction(() => window.__pdfReady);
      assert.equal(await test.page.evaluate(() => window.__pdfSharedPromise && window.__pdfSameLibrary), true);
      assert.equal(test.assetRequests.filter(url => url === '/static/pdf.min.js').length, 1);
      assert.equal(await test.page.locator('script[src="/static/pdf.min.js"]').count(), 1);
      assert.equal(await test.page.evaluate(() => pdfjsLib.GlobalWorkerOptions.workerSrc), '/static/pdf.worker.min.js');
      await test.page.evaluate(() => ensurePdfLibrary());
      assert.equal(test.assetRequests.filter(url => url === '/static/pdf.min.js').length, 1);
      return { requests: 1, workerConfigured: true };
    });

    await runCase(browser, 'early file preview controls before Markdown arrives', {}, async test => {
      await cachedContentIsImmediate(test, 'early file preview');
      assert.equal(await test.page.evaluate(() => typeof window.marked), 'undefined', 'Markdown response must still be held');
      const file = test.page.locator('#all-files-list .file-item').filter({ hasText: '緩存數學.docx' });
      await file.locator('.file-more-btn').click();
      await file.locator('.preview-file').click();
      assert.equal(await test.page.locator('#file-preview-pane').isVisible(), true, 'cached file menu must open preview immediately');
      await test.page.locator('#file-preview-pane-fullscreen').click();
      assert.equal(await test.page.locator('#file-panel').evaluate(panel => panel.classList.contains('is-preview-fullscreen')), true, 'fullscreen must work before Markdown arrives');
      await test.page.locator('#file-preview-back').click();
      assert.equal(await test.page.locator('#file-preview-pane').isVisible(), false, 'back must work before Markdown arrives');
      return {};
    });

    await runCase(browser, 'cached text preview updates in place when files arrive', {}, async test => {
      await cachedContentIsImmediate(test, 'cached text preview');
      const file = test.page.locator('#all-files-list .file-item').filter({ hasText: '緩存數學.docx' });
      await file.locator('.file-more-btn').click();
      await file.locator('.preview-file').click();
      const preview = test.page.locator('#file-preview-content');
      assert.match(await preview.textContent(), /背景更新/);
      assert.doesNotMatch(await preview.textContent(), /[暫暂][無无]已提取/);
      await test.page.locator('#file-preview-pane-fullscreen').click();
      const generation = await test.page.evaluate(() => filePreviewGeneration);
      test.setData({ files: FILES.map(file => file.id === 'fixture-math' ? { ...file, extracted_text: '後台讀取的 Word 原文內容' } : file) });
      test.releaseApi();
      await test.page.locator('#file-preview-content .file-preview-text').filter({ hasText: '後台讀取的 Word 原文內容' }).waitFor();
      assert.equal(await test.page.locator('#file-preview-pane').isVisible(), true);
      assert.equal(await test.page.locator('#file-panel').evaluate(panel => panel.classList.contains('is-preview-fullscreen')), true, 'refresh must preserve fullscreen');
      assert.equal(await test.page.evaluate(() => filePreviewGeneration), generation, 'refresh must update the active preview without reopening');
      assert.equal(await preview.locator('button').count(), 0, 'successful update removes the retry button');
      assert.equal(await test.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).files.some(file => Object.hasOwn(file, 'extracted_text')), CACHE_KEY), false, 'startup cache contains metadata only');
      await test.page.locator('#file-preview-back').click();
      assert.equal(await test.page.locator('#file-preview-pane').isVisible(), false);
      return { updatedWithoutReopening: true };
    });

    await runCase(browser, 'unchanged background data preserves cached conversation and list DOM', {}, async test => {
      await cachedContentIsImmediate(test, 'unchanged background data');
      await test.page.evaluate(() => {
        window.__cachedNodes = ['#all-files-list .file-item', '#history-list .history-item', '#messages-container .message-row']
          .map(selector => document.querySelector(selector));
      });
      test.release();
      await test.page.waitForLoadState('load');
      await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
      assert.deepEqual(await test.page.evaluate(() => ['#all-files-list .file-item', '#history-list .history-item', '#messages-container .message-row']
        .map((selector, index) => document.querySelector(selector) === window.__cachedNodes[index])), [true, true, true]);
      return { noUnnecessaryDomRebuild: true };
    });

    await runCase(browser, 'long cached history remains scrollable with offscreen rendering', {}, async test => {
      await cachedContentIsImmediate(test, 'long cached history');
      await test.page.evaluate(() => {
        conversationHistory = Array.from({ length: 100 }, (_, index) => ({
          id: 'scroll-fixture-' + index, title: '合成歷史記錄 ' + index,
          message_count: 2, updated_at: new Date(Date.now() - index * 60000).toISOString()
        }));
        renderConversationHistory();
      });
      const last = test.page.locator('#history-list .history-item').last();
      await last.scrollIntoViewIfNeeded();
      assert.equal(await last.isVisible(), true);
      assert.equal(await last.locator('.rename-btn').isVisible(), true);
      assert.match(await last.textContent(), /合成歷史記錄 99/);
      assert.equal(await test.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      return { allHistoryRowsAccessible: true };
    });

    await runCase(browser, 'teacher stream completes before background conversation cache refresh', { mockChat: true }, async test => {
      await cachedContentIsImmediate(test, 'teacher stream cache');
      test.release();
      await test.page.waitForLoadState('load');
      await test.page.waitForFunction(() => !loadFilesAbortController && !loadHistoryAbortController);
      test.reset();
      await test.page.locator('#ai-skill-select').selectOption('chemistry-teacher');
      await test.page.locator('#ai-text-input').fill('合成問題：乙烯與溴是甚麼反應？');
      await test.page.locator('#ai-send-btn').click();
      await test.page.waitForFunction(() => !isLoading);
      assert.equal(await test.page.locator('#ai-send-btn').isEnabled(), true, 'composer must be usable while cache refresh is held');
      assert.match(await test.page.locator('#messages-container').textContent(), /合成化學老師回答：觀察碳碳雙鍵/);
      assert.equal(await test.page.locator('.answer-skill-label').last().textContent(), '化學老師');
      assert.equal(test.count('/api/chat'), 1);
      assert.equal(test.count('/api/session'), 1, 'completed stream refreshes the canonical conversation once');
      const before = await test.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).currentConversation.messages.at(-1).content, CACHE_KEY);
      assert.doesNotMatch(before, /合成化學老師回答/, 'the canonical session response is still held');
      test.releaseApi();
      await test.page.waitForFunction(key => JSON.parse(sessionStorage.getItem(key)).currentConversation.messages.at(-1).content.includes('合成化學老師回答'), CACHE_KEY);
      const cachedAnswer = await test.page.evaluate(key => JSON.parse(sessionStorage.getItem(key)).currentConversation.messages.at(-1), CACHE_KEY);
      assert.equal(cachedAnswer.skill_id, 'chemistry-teacher');
      assert.equal(cachedAnswer.skill_context.status, 'no_relevant_sources');
      assert.equal(await test.page.locator('.answer-skill-label').last().textContent(), '化學老師');
      return { composerReadyBeforeCacheResponse: true, canonicalAnswerCached: true };
    });

    const rejectedCaches = [
      ['no startup cache', { cache: null }],
      ['corrupt startup cache', { cache: '{broken json' }],
      ['sessionStorage blocked', { storageBlocked: true }],
      ['cache older than 12 hours', { cache: snapshot({ savedAt: Date.now() - 13 * 60 * 60 * 1000 }) }],
      ['cache from another session', { cacheKey: 'houkong.startup.v1:another-session' }],
      ['cache from another account', { cache: snapshot({ userInfo: { ...USER, account: 'another-account' } }) }],
    ];
    for (const [name, options] of rejectedCaches) {
      await runCase(browser, name, options, async test => {
        const appMs = await shellIsImmediate(test, name);
        assert.doesNotMatch(await test.page.locator('#history-list').textContent(), /緩存對話標題/, name + ': private history must not hydrate');
        assert.doesNotMatch(await test.page.locator('#all-files-list').textContent(), /緩存化學.pdf/, name + ': private files must not hydrate');
        assert.doesNotMatch(await test.page.locator('#messages-container').textContent(), /緩存中的問題/, name + ': private messages must not hydrate');
        test.release();
        await test.page.waitForLoadState('load');
        await test.page.locator('#all-files-list .file-item').first().waitFor();
        assert.equal(await test.page.locator('#app').isVisible(), true);
        return { appMs };
      });
    }
    console.log(JSON.stringify({ passed: true, scenarios: results.length, results }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
