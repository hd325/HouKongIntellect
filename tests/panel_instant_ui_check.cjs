// Offline browser regression: real production HTML/assets, synthetic accounts/API only.
// Install Playwright, then run: node tests/panel_instant_ui_check.cjs
// Click-to-paint is measured from the real click event to the second animation frame.
// Navigation FCP is reported separately; a 100 ms interaction is not a 100 ms cold load.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const ORIGIN = 'http://127.0.0.1:8000';
const SESSION = 'panel-fixture-session';
const ACCOUNT = 'panel-fixture-account';
const USER = { account: ACCOUNT, name: '面板隔離測試', class: '高二測試班', role: 'teacher' };
const PATHS = ['/api/rag/status', '/api/metrics/summary', '/api/reviews'];
const key = (kind, session = SESSION) => 'houkong.panel.v1:' + kind + ':' + session;
const envelope = (data, overrides = {}) => ({ version: 1, savedAt: Date.now(), account: ACCOUNT, data, ...overrides });
const KNOWLEDGE = { available: true, enabled: true, document_count: 37, total_chunks: 147, backend: 'local', dim: 384 };
const SUMMARY = {
  days: 30, start_date: '2026-09-19', end_date: '2026-10-18', record_count: 9, minimum_records: 5, data_insufficient: false,
  summary: { questions: 9, empty_rate: 1 / 9, p50_latency_ms: 42, p95_latency_ms: 82 },
  daily: [{ date: '2026-10-17', questions: 4, empty_rate: 0 }, { date: '2026-10-18', questions: 5, empty_rate: 0.2 }],
  latency_histogram: [{ label: '0–100', min_ms: 0, max_ms: 100, count: 9 }],
  filename_record_count: 9, subject_data_insufficient: false, subject_distribution_available: true,
  subjects: [{ subject: '化學', count: 6 }, { subject: '數學', count: 3 }],
};
const FILE = { id: 'synthetic-review-file', name: '合成待審化學資料.pdf', owner: 'fixture-student', owner_name: '測試學生', owner_class: '高二測試班', audience: 'school', size: 1234, uploaded_at: '2026-10-18T08:00:00Z', publisher_role: 'student', review_status: 'pending' };
const REVIEWS = { is_teacher: true, reviewing: [FILE], published: [{ ...FILE, id: 'synthetic-published-file', name: '合成已發布化學資料.pdf', review_status: 'published' }] };
const METRICS = { knowledge: KNOWLEDGE, summary: SUMMARY };
const results = [];
const filter = process.env.PANEL_CASE ? new RegExp(process.env.PANEL_CASE, 'i') : null;

function gate() {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  return { promise, release };
}

async function fixture(browser, options = {}) {
  const context = await browser.newContext({ viewport: options.viewport || { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [], requests = [], unexpected = [];
  const states = new Map(PATHS.map(endpoint => [endpoint, { gate: gate(), status: 200, data: endpoint.endsWith('/status') ? KNOWLEDGE : endpoint.endsWith('/summary') ? SUMMARY : REVIEWS }]));
  const sessionGate = gate();
  if (!options.holdSession) sessionGate.release();
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(({ session, user, caches, blocked }) => {
    if (!localStorage.getItem('__panelFixtureSeeded')) {
      localStorage.setItem('__panelFixtureSeeded', '1');
      localStorage.setItem('sessionId', session);
      localStorage.setItem('userInfo', JSON.stringify(user));
      sessionStorage.setItem('houkong.startup.v1:' + session, JSON.stringify({ version: 1, savedAt: Date.now(), userInfo: user, history: [], files: [], currentConversation: null }));
      for (const [name, value] of Object.entries(caches)) sessionStorage.setItem(name, typeof value === 'string' ? value : JSON.stringify(value));
    }
    if (blocked) Object.defineProperty(window, 'sessionStorage', { configurable: true, get() { throw new DOMException('Synthetic storage denial', 'SecurityError'); } });
    window.__panelClickFrames = [];
    document.addEventListener('click', event => {
      const opener = event.target.closest('#open-metrics, #open-reviews');
      if (!opener) return;
      const started = performance.now();
      const kind = opener.id === 'open-metrics' ? 'metrics' : 'reviews';
      const sample = { kind, started };
      window.__panelClickFrames.push(sample);
      requestAnimationFrame(() => {
        const panel = document.getElementById(kind === 'metrics' ? 'metrics-panel' : 'review-overlay');
        sample.firstFrameMs = performance.now() - started;
        sample.visibleAtFirstFrame = !!panel && !panel.hidden && panel.getClientRects().length > 0;
        sample.textAtFirstFrame = panel?.innerText || '';
        sample.metricsContentVisible = kind !== 'metrics' || !document.getElementById('metrics-content').hidden;
        requestAnimationFrame(() => { sample.paintUpperBoundMs = performance.now() - started; });
      });
    }, true);
  }, {
    session: SESSION, user: USER, blocked: !!options.storageBlocked,
    caches: Object.hasOwn(options, 'caches') ? options.caches : { [key('metrics')]: envelope(METRICS), [key('reviews')]: envelope(REVIEWS) },
  });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== ORIGIN) return route.fulfill({ contentType: 'text/css', body: '' });
    if (url.pathname.startsWith('/api/')) {
      const endpoint = url.pathname;
      requests.push({ endpoint, session: request.headers()['x-session-id'], method: request.method() });
      if (PATHS.includes(endpoint)) {
        const state = states.get(endpoint);
        await state.gate.promise;
        if (state.status === 0) return route.abort('failed');
        return route.fulfill({ status: state.status, json: state.status === 200 ? { code: 200, data: state.data } : { code: state.status, message: 'Synthetic background failure' } });
      }
      if (endpoint === '/api/session') {
        await sessionGate.promise;
        return route.fulfill({ json: { code: 200, data: { user_info: USER, current_conversation: null } } });
      }
      const data = endpoint === '/api/files' ? { files: [] } : endpoint === '/api/conversations' ? { conversations: [] } : endpoint === '/api/skills' ? { skills: [] } : null;
      if (data) return route.fulfill({ json: { code: 200, data } });
      unexpected.push({ endpoint, method: request.method() });
      return route.fulfill({ status: 404, json: { code: 404, message: 'Unknown synthetic endpoint' } });
    }
    if (url.pathname === '/' || url.pathname === '/index.html') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(ROOT, 'index.html')) });
    const asset = path.resolve(ROOT, decodeURIComponent(url.pathname.replace(/^\/static\//, '')));
    if (!asset.startsWith(ROOT + path.sep) || !fs.existsSync(asset) || !fs.statSync(asset).isFile()) return route.fulfill({ status: 404, body: '' });
    const contentType = { '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' }[path.extname(asset)] || 'application/octet-stream';
    return route.fulfill({ contentType, body: fs.readFileSync(asset) });
  });
  return {
    page, errors, requests, unexpected,
    count(endpoint) { return requests.filter(item => item.endpoint === endpoint).length; },
    release(endpoint, data, status = 200) {
      const state = states.get(endpoint); if (data !== undefined) state.data = data; state.status = status; state.gate.release();
    },
    releaseAll() { for (const state of states.values()) state.gate.release(); sessionGate.release(); },
    hold(endpoint, data, status = 200) {
      const old = states.get(endpoint); old.gate.release();
      states.set(endpoint, { gate: gate(), data: data === undefined ? old.data : data, status });
    },
    async open() {
      await page.goto(ORIGIN, { waitUntil: 'domcontentloaded' });
      await page.locator('#app').waitFor({ state: 'visible' });
      await page.waitForFunction(() => typeof window.prefetchMetricsPanel === 'function' && typeof window.prefetchReviewPanel === 'function');
    },
    async close() { for (const state of states.values()) state.gate.release(); sessionGate.release(); await context.close(); },
  };
}

async function clickPaint(test, kind, expected) {
  const id = kind === 'metrics' ? '#open-metrics' : '#open-reviews';
  const before = await test.page.evaluate(() => window.__panelClickFrames.length);
  await test.page.locator(id).click();
  await test.page.waitForFunction(index => Number.isFinite(window.__panelClickFrames[index]?.paintUpperBoundMs), before);
  const sample = await test.page.evaluate(index => window.__panelClickFrames[index], before);
  assert.equal(sample.visibleAtFirstFrame, true, kind + ' visible at first frame');
  assert.equal(sample.metricsContentVisible, true, kind + ' content visible at first frame');
  assert.ok(sample.paintUpperBoundMs < 100, `${kind} click to painted content upper bound ${sample.paintUpperBoundMs.toFixed(1)} ms must be below 100 ms`);
  assert.doesNotMatch(sample.textAtFirstFrame, /正在載入|正在加载|載入中|加载中|加載中/, kind + ' must not show a loading screen');
  if (expected) assert.match(sample.textAtFirstFrame, expected, kind + ' actual content at first frame');
  return { firstFrameMs: sample.firstFrameMs, clickToPaintUpperBoundMs: sample.paintUpperBoundMs };
}

async function closePanel(test, kind) {
  await test.page.locator(kind === 'metrics' ? '#metrics-close' : '#review-close').click();
  assert.equal(await test.page.locator(kind === 'metrics' ? '#metrics-panel' : '#review-overlay').isVisible(), false);
  assert.equal(await test.page.evaluate(() => document.activeElement?.id), kind === 'metrics' ? 'open-metrics' : 'open-reviews');
}

async function metricsValues(test, documents = '37', questions = '9') {
  assert.equal(await test.page.locator('#metrics-documents').textContent(), documents);
  assert.equal(await test.page.locator('#metrics-questions').textContent(), questions);
}

async function scenario(browser, name, options, check) {
  if (filter && !filter.test(name)) return;
  const test = await fixture(browser, options);
  try {
    await test.open();
    const detail = await check(test);
    assert.deepEqual(test.errors, [], name + ': no uncaught browser errors');
    assert.deepEqual(test.unexpected, [], name + ': no unexpected API calls or writes');
    results.push({ name, passed: true, ...detail });
  } finally { await test.close(); }
}

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined) });
  try {
    await scenario(browser, 'cached panels render at first paint and preserve DOM on reopen', {}, async test => {
      const metrics = await clickPaint(test, 'metrics', /37/); await metricsValues(test);
      await test.page.evaluate(() => { window.__fixtureChart = document.querySelector('#metrics-volume-chart svg'); });
      assert.equal(await test.page.locator('#metrics-volume-chart svg').count(), 1);
      await closePanel(test, 'metrics');
      const reviews = await clickPaint(test, 'reviews', /合成待審化學資料/);
      await test.page.evaluate(() => { window.__fixtureReview = document.querySelector('#review-list .review-card'); });
      await closePanel(test, 'reviews');
      const repeated = [];
      for (let i = 0; i < 3; i++) {
        repeated.push(await clickPaint(test, 'metrics', /37/));
        assert.equal(await test.page.evaluate(() => window.__fixtureChart === document.querySelector('#metrics-volume-chart svg')), true, 'unchanged chart node must survive reopening');
        await closePanel(test, 'metrics');
        repeated.push(await clickPaint(test, 'reviews', /合成待審化學資料/));
        assert.equal(await test.page.evaluate(() => window.__fixtureReview === document.querySelector('#review-list .review-card')), true, 'unchanged review card must survive reopening');
        await closePanel(test, 'reviews');
      }
      return { metrics, reviews, repeated };
    });

    await scenario(browser, 'actual page refresh restores both cached panels before API responses', {}, async test => {
      await clickPaint(test, 'metrics', /37/); await closePanel(test, 'metrics');
      await test.page.reload({ waitUntil: 'domcontentloaded' });
      const metrics = await clickPaint(test, 'metrics', /37/); await metricsValues(test); await closePanel(test, 'metrics');
      const reviews = await clickPaint(test, 'reviews', /合成待審化學資料/);
      const navigationFcpMs = await test.page.evaluate(() => performance.getEntriesByName('first-contentful-paint')[0]?.startTime ?? null);
      return { metrics, reviews, navigationFcpMs };
    });

    await scenario(browser, 'hidden prefetch shares in-flight requests with opening and closing', { caches: {} }, async test => {
      await test.page.evaluate(() => { void window.prefetchMetricsPanel(); void window.prefetchMetricsPanel(); void window.prefetchReviewPanel(); void window.prefetchReviewPanel(); });
      await test.page.waitForFunction(() => performance.now() > 350);
      assert.equal(await test.page.locator('#metrics-panel').isVisible(), false);
      assert.equal(await test.page.locator('#review-overlay').isVisible(), false);
      await clickPaint(test, 'metrics'); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews'); await closePanel(test, 'reviews');
      for (const endpoint of PATHS) assert.equal(test.count(endpoint), 1, endpoint + ': prefetch and open must share one request');
      test.releaseAll();
      await test.page.waitForFunction(keys => keys.every(name => !!sessionStorage.getItem(name)), [key('metrics'), key('reviews')]);
      const metrics = await clickPaint(test, 'metrics', /37/); await metricsValues(test); await closePanel(test, 'metrics');
      const reviews = await clickPaint(test, 'reviews', /合成待審化學資料/);
      return { metrics, reviews, requestsShared: true };
    });

    await scenario(browser, 'knowledge updates independently while summary response is held', { caches: {} }, async test => {
      await clickPaint(test, 'metrics');
      test.release('/api/rag/status');
      await test.page.waitForFunction(() => document.getElementById('metrics-documents').textContent === '37');
      assert.equal(await test.page.locator('#metrics-questions').textContent(), '—');
      assert.equal(await test.page.locator('#metrics-content').isVisible(), true);
      test.release('/api/metrics/summary');
      await test.page.waitForFunction(() => document.getElementById('metrics-questions').textContent === '9');
      await metricsValues(test);
      const cached = await test.page.evaluate(name => JSON.parse(sessionStorage.getItem(name)), key('metrics'));
      assert.equal(cached.data.knowledge.document_count, 37);
      assert.equal(cached.data.summary.record_count, 9);
      return { independentEndpoints: true };
    });

    await scenario(browser, 'successful background refresh updates content and stored snapshot', {}, async test => {
      await clickPaint(test, 'metrics', /37/);
      test.release('/api/rag/status', { ...KNOWLEDGE, document_count: 41 });
      test.release('/api/metrics/summary', { ...SUMMARY, record_count: 12, summary: { ...SUMMARY.summary, questions: 12 } });
      await test.page.waitForFunction(() => document.getElementById('metrics-questions').textContent === '12');
      await metricsValues(test, '41', '12'); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews', /合成待審化學資料/);
      test.release('/api/reviews', { ...REVIEWS, reviewing: [{ ...FILE, name: '後台更新待審資料.pdf' }] });
      await test.page.locator('.review-file-name').filter({ hasText: '後台更新待審資料' }).waitFor();
      const snapshots = await test.page.evaluate(names => names.map(name => JSON.parse(sessionStorage.getItem(name))), [key('metrics'), key('reviews')]);
      assert.equal(snapshots[0].data.knowledge.document_count, 41);
      assert.equal(snapshots[1].data.reviewing[0].name, '後台更新待審資料.pdf');
      return { contentAndCacheRefreshed: true };
    });

    await scenario(browser, 'background errors preserve cached data and chart nodes', {}, async test => {
      await clickPaint(test, 'metrics', /37/);
      await test.page.evaluate(() => { window.__fixtureChart = document.querySelector('#metrics-volume-chart svg'); });
      test.release('/api/rag/status', undefined, 503); test.release('/api/metrics/summary', undefined, 503);
      await test.page.locator('#metrics-error').waitFor({ state: 'visible' });
      await metricsValues(test);
      assert.equal(await test.page.evaluate(() => window.__fixtureChart === document.querySelector('#metrics-volume-chart svg')), true);
      await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews', /合成待審化學資料/);
      test.release('/api/reviews', undefined, 503);
      await test.page.waitForFunction(() => document.getElementById('review-feedback').classList.contains('is-error'));
      assert.match(await test.page.locator('#review-list').textContent(), /合成待審化學資料/);
      assert.equal(await test.page.evaluate(name => JSON.parse(sessionStorage.getItem(name)).data.reviewing.length, key('reviews')), 1);
      return { preservedCachedData: true };
    });

    await scenario(browser, 'unknown review result is not shown as a confirmed empty collection', { caches: {} }, async test => {
      const timing = await clickPaint(test, 'reviews');
      assert.doesNotMatch(await test.page.locator('#review-list').textContent(), /暫無審核中的檔案|暫無已發布檔案/);
      assert.equal(await test.page.locator('#review-count-reviewing').textContent(), '—');
      test.release('/api/reviews', { is_teacher: true, reviewing: [], published: [] });
      await test.page.waitForFunction(() => document.getElementById('review-count-reviewing').textContent === '0');
      assert.match(await test.page.locator('#review-list').textContent(), /暫無審核中的檔案/);
      return { timing, unknownAndEmptyDistinct: true };
    });

    await scenario(browser, 'confirmed empty cached collection appears immediately', { caches: { [key('reviews')]: envelope({ is_teacher: true, reviewing: [], published: [] }) } }, async test => {
      const timing = await clickPaint(test, 'reviews', /暫無審核中的檔案/);
      assert.equal(await test.page.locator('#review-count-reviewing').textContent(), '0');
      return { timing };
    });

    const invalid = [
      ['expired cache', value => envelope(value, { savedAt: Date.now() - 13 * 60 * 60 * 1000 })],
      ['other account cache', value => envelope(value, { account: 'different-account' })],
      ['future cache', value => envelope(value, { savedAt: Date.now() + 60 * 1000 })],
      ['corrupt cache', () => '{bad-json'],
    ];
    for (const [name, wrap] of invalid) {
      await scenario(browser, name + ' does not hydrate private content', { caches: { [key('metrics')]: wrap(METRICS), [key('reviews')]: wrap(REVIEWS) } }, async test => {
        await clickPaint(test, 'metrics');
        assert.equal(await test.page.locator('#metrics-documents').textContent(), '—'); await closePanel(test, 'metrics');
        await clickPaint(test, 'reviews');
        assert.doesNotMatch(await test.page.locator('#review-list').textContent(), /合成待審化學資料/);
        test.releaseAll();
        await test.page.locator('.review-file-name').filter({ hasText: '合成待審化學資料' }).waitFor();
        return { rejectedUntrustedSnapshot: true };
      });
    }

    await scenario(browser, 'storage denied keeps both panels interactive and refreshes in memory', { storageBlocked: true }, async test => {
      const metrics = await clickPaint(test, 'metrics');
      test.release('/api/rag/status'); test.release('/api/metrics/summary');
      await test.page.waitForFunction(() => document.getElementById('metrics-questions').textContent === '9');
      await closePanel(test, 'metrics');
      const reviews = await clickPaint(test, 'reviews'); test.release('/api/reviews');
      await test.page.locator('.review-file-name').filter({ hasText: '合成待審化學資料' }).waitFor();
      await closePanel(test, 'reviews');
      await clickPaint(test, 'metrics', /37/); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews', /合成待審化學資料/);
      return { metrics, reviews, memoryFallback: true };
    });

    await scenario(browser, 'stale session response never populates another session or cache', { caches: {}, holdSession: true }, async test => {
      await test.page.evaluate(() => { void window.prefetchMetricsPanel(); void window.prefetchReviewPanel(); });
      await test.page.waitForFunction(() => performance.now() > 350);
      for (const endpoint of PATHS) assert.equal(test.count(endpoint), 1);
      await test.page.evaluate(() => { sessionId = 'replacement-fixture-session'; localStorage.setItem('sessionId', sessionId); localStorage.setItem('userInfo', JSON.stringify({ account: 'replacement-fixture-account', name: '替換帳號' })); });
      const responses = Promise.all(PATHS.map(endpoint => test.page.waitForResponse(response => new URL(response.url()).pathname === endpoint)));
      test.releaseAll();
      // Subsequent replacement-account requests stay held; only the original responses arrive.
      for (const endpoint of PATHS) test.hold(endpoint);
      await responses;
      await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await clickPaint(test, 'metrics');
      assert.equal(await test.page.locator('#metrics-documents').textContent(), '—'); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews');
      assert.doesNotMatch(await test.page.locator('#review-list').textContent(), /合成待審化學資料/);
      const cached = await test.page.evaluate(names => names.map(name => sessionStorage.getItem(name)), [key('metrics', 'replacement-fixture-session'), key('reviews', 'replacement-fixture-session')]);
      assert.deepEqual(cached, [null, null]);
      return { staleResponsesRejected: true };
    });

    await scenario(browser, 'same session account replacement rejects stale panel response', { caches: {}, holdSession: true }, async test => {
      await test.page.evaluate(() => { void window.prefetchMetricsPanel(); void window.prefetchReviewPanel(); });
      await test.page.waitForFunction(() => performance.now() > 350);
      for (const endpoint of PATHS) assert.equal(test.count(endpoint), 1);
      await test.page.evaluate(() => { localStorage.setItem('userInfo', JSON.stringify({ account: 'replacement-fixture-account', name: '替換帳號' })); });
      const responses = Promise.all(PATHS.map(endpoint => test.page.waitForResponse(response => new URL(response.url()).pathname === endpoint)));
      // Keep /session held so it cannot replace the deliberately changed account.
      for (const endpoint of PATHS) test.release(endpoint);
      for (const endpoint of PATHS) test.hold(endpoint);
      await responses;
      await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.deepEqual(await test.page.evaluate(names => names.map(name => sessionStorage.getItem(name)), [key('metrics'), key('reviews')]), [null, null]);
      await clickPaint(test, 'metrics');
      assert.equal(await test.page.locator('#metrics-documents').textContent(), '—'); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews');
      assert.doesNotMatch(await test.page.locator('#review-list').textContent(), /合成待審化學資料/);
      return { accountReplacementRejectedStaleResponses: true };
    });

    await scenario(browser, 'session invalidation clears panel caches and closes dialogs', {}, async test => {
      await clickPaint(test, 'metrics', /37/); await closePanel(test, 'metrics');
      await clickPaint(test, 'reviews', /合成待審化學資料/);
      await test.page.evaluate(value => invalidateStartupSession(value), SESSION);
      assert.equal(await test.page.locator('#review-overlay').isVisible(), false);
      assert.equal(await test.page.locator('#metrics-panel').isVisible(), false);
      assert.equal(await test.page.locator('#login-page').isVisible(), true);
      assert.deepEqual(await test.page.evaluate(names => names.map(name => sessionStorage.getItem(name)), [key('metrics'), key('reviews')]), [null, null]);
      test.releaseAll();
      await test.page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.deepEqual(await test.page.evaluate(names => names.map(name => sessionStorage.getItem(name)), [key('metrics'), key('reviews')]), [null, null]);
      return { invalidationErasesPrivateSnapshots: true };
    });

    for (const width of [390, 320]) {
      await scenario(browser, `${width}px viewport has no horizontal overflow and closes with restored focus`, { viewport: { width, height: 844 } }, async test => {
        const metrics = await clickPaint(test, 'metrics', /37/);
        assert.equal(await test.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.equal(await test.page.locator('#metrics-panel').evaluate(el => el.scrollWidth <= innerWidth), true);
        await test.page.keyboard.press('Escape');
        assert.equal(await test.page.evaluate(() => document.activeElement?.id), 'open-metrics');
        const reviews = await clickPaint(test, 'reviews', /合成待審化學資料/);
        assert.equal(await test.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.equal(await test.page.locator('#review-overlay').evaluate(el => el.scrollWidth <= innerWidth), true);
        await test.page.keyboard.press('Escape');
        assert.equal(await test.page.evaluate(() => document.activeElement?.id), 'open-reviews');
        return { metrics, reviews, noOverflow: true };
      });
    }
    console.log(JSON.stringify({ passed: true, scenarios: results.length, results }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
