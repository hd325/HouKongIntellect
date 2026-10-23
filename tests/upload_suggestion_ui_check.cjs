// Offline browser checks: production classification UI, synthetic files and mocked API only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const style = source.match(/<style>([\s\S]*?)<\/style>/)[1];
const ui = source.slice(source.indexOf('        // ===================== 檔案分類彈窗'),
  source.indexOf('        panelFileInput.addEventListener', source.indexOf('        // ===================== 檔案分類彈窗')));
const modal = source.slice(source.indexOf('    <div class="categorize-dialog-overlay"'),
  source.indexOf('    <!-- ==================== 檔案預覽彈窗'));
const html = `<!doctype html><html><head><meta charset="utf-8"><style>${style}</style></head><body>${modal}<script>
  let sessionId = 'suggestion-fixture', logoutInProgress = false;
  const API_BASE = location.origin + '/api';
  function formatFileSize(size) { return size + ' B'; }
  function showToast() {} function loadFiles() {} function refreshReviewPanel() {}
  ${ui}
</script></body></html>`;
const calendar = { audience: 'school', study_period: 'none', subject: 'other', material_type: 'other',
  material_custom: '校曆', notes: '校曆' };
const results = [];

async function fixture(browser, data = calendar, profile = { role: 'student' }) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 1000 } });
  const page = await context.newPage();
  const errors = [], uploads = [];
  let release;
  const held = new Promise(resolve => { release = resolve; });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(value => localStorage.setItem('userInfo', JSON.stringify(value)), profile);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/upload/analyze') {
      await held;
      return route.fulfill({ contentType: 'application/x-ndjson',
        body: JSON.stringify({ type: 'result', data }) + '\n' });
    }
    if (url.pathname === '/api/upload') {
      uploads.push(route.request().url());
      return route.fulfill({ json: { code: 500 } });
    }
    return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
  });
  await page.goto('http://127.0.0.1:8000/');
  await page.evaluate(() => handleFileSelect([new File(['synthetic calendar'], 'fixture.pdf', { type: 'application/pdf' })]));
  return { page, errors, uploads, release, close: () => context.close(),
    async done() { release(); await page.waitForFunction(() => !categorizeSession.items[0].analyzing); } };
}

async function check(name, body) {
  await body();
  results.push(name);
  console.log('PASS ' + name);
}

(async () => {
  const browser = await chromium.launch({ headless: true,
    channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined) });
  try {
    await check('calendar suggestions fill school / other / custom and omit duplicate notes; review still requires confirmation', async () => {
      const f = await fixture(browser);
      try {
        assert.equal(await f.page.inputValue('#audience-select'), 'personal');
        await f.done();
        for (const [id, value] of Object.entries({ 'audience-select': 'school', 'subject-select': 'other',
          'study-period-select': 'none', 'material-type-select': 'other', 'material-custom-input': '校曆', 'notes-input': '' })) {
          assert.equal(await f.page.inputValue('#' + id), value, id);
        }
        assert.equal(await f.page.locator('#categorize-confirm-btn').textContent(), '提交審核');
        assert.equal(await f.page.locator('#categorize-confirm-btn').isEnabled(), true);
        assert.equal(await f.page.locator('#target-class-field').isVisible(), false);
        assert.equal(f.uploads.length, 0);
        assert.deepEqual(f.errors, []);
      } finally { await f.close(); }
    });
    await check('manual personal scope and manually entered fields survive delayed AI response', async () => {
      const f = await fixture(browser);
      try {
        await f.page.selectOption('#audience-select', 'personal');
        await f.page.selectOption('#subject-select', 'math');
        await f.page.selectOption('#material-type-select', 'other');
        await f.page.fill('#material-custom-input', '人工類型');
        await f.page.fill('#notes-input', '人工類型');
        await f.done();
        assert.equal(await f.page.inputValue('#audience-select'), 'personal');
        assert.equal(await f.page.inputValue('#subject-select'), 'math');
        assert.equal(await f.page.inputValue('#material-custom-input'), '人工類型');
        assert.equal(await f.page.inputValue('#notes-input'), '人工類型');
        assert.equal(await f.page.locator('#categorize-confirm-btn').textContent(), '確認上傳');
        assert.equal(f.uploads.length, 0);
        assert.deepEqual(f.errors, []);
      } finally { await f.close(); }
    });
    for (const audience of ['', 'all', undefined]) {
      await check('unknown scope preserves personal default: ' + String(audience), async () => {
        const f = await fixture(browser, { ...calendar, audience });
        try {
          await f.done();
          assert.equal(await f.page.inputValue('#audience-select'), 'personal');
          assert.deepEqual(f.errors, []);
        } finally { await f.close(); }
      });
    }
    await check('teacher grade suggestion requires an explicit publishing class', async () => {
      const f = await fixture(browser, { ...calendar, audience: 'grade' }, { role: 'teacher', publish_classes: ['高二甲班'] });
      try {
        await f.done();
        assert.equal(await f.page.locator('#target-class-field').isVisible(), true);
        assert.equal(await f.page.locator('#target-class-label').textContent(), '年級所屬班級');
        assert.equal(await f.page.locator('#categorize-confirm-btn').isEnabled(), false);
        await f.page.selectOption('#target-class-select', '高二甲班');
        assert.equal(await f.page.locator('#categorize-confirm-btn').isEnabled(), true);
        assert.equal(await f.page.locator('#categorize-confirm-btn').textContent(), '確認上傳');
        assert.equal(f.uploads.length, 0);
        assert.deepEqual(f.errors, []);
      } finally { await f.close(); }
    });
    await check('changing a material type removes only redundant AI notes', async () => {
      const f = await fixture(browser, { ...calendar, material_custom: '學校通知', notes: '校曆' });
      try {
        await f.done();
        assert.equal(await f.page.inputValue('#notes-input'), '校曆');
        await f.page.fill('#material-custom-input', '校曆');
        assert.equal(await f.page.inputValue('#notes-input'), '');
        await f.page.fill('#notes-input', '人工備註');
        await f.page.fill('#material-custom-input', '人工備註');
        assert.equal(await f.page.inputValue('#notes-input'), '人工備註');
        assert.deepEqual(f.errors, []);
      } finally { await f.close(); }
    });
    await check('standard material labels also suppress duplicate AI notes', async () => {
      const f = await fixture(browser, { ...calendar, material_type: 'homework', material_custom: '', notes: '課後作業' });
      try {
        await f.done();
        assert.equal(await f.page.inputValue('#notes-input'), '');
        assert.deepEqual(f.errors, []);
      } finally { await f.close(); }
    });
  } finally { await browser.close(); }
  console.log(`Ran ${results.length} browser checks: OK (all API calls mocked; zero cloud OCR calls)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
