// Offline UI contract: production HTML/assets, synthetic account, files and SSE only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const ORIGIN = 'http://127.0.0.1:8000';
const OUTPUT = path.join(ROOT, 'work', 'file-agent-2026-10-22', 'ui-verification');
const SESSION = 'file-agent-ui-fixture';
const USER = { account: 'file-agent-fixture-owner', name: '檔案測試', role: 'student' };
const PDF = Buffer.from('%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF');
const ARTIFACTS = [
  { id: 'fixture-docx-v1', filename: '假期整理.docx', file_size: 2417, version: 1, has_original: true, description: '已保留未要求修改的段落。' },
  { id: 'fixture-xlsx-v1', filename: '假期列表.xlsx', file_size: 3872, version: 1, has_original: true, description: '假期與日期表格。' },
];

(async () => {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined) });
  const results = [], errors = [], chatRequests = [], fileRequests = [];
  let conversation = { id: 'file-agent-fixture-conversation', messages: [] };
  try {
    fs.mkdirSync(OUTPUT, { recursive: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(({ session, user }) => {
      if (window.top !== window) return;
      localStorage.setItem('sessionId', session);
      localStorage.setItem('userInfo', JSON.stringify(user));
      window.__fileStatuses = [];
      document.addEventListener('DOMContentLoaded', () => {
        new MutationObserver(() => document.querySelectorAll('.file-task-status:not([hidden])').forEach(element => {
          const status = element.dataset.status;
          if (status && window.__fileStatuses.at(-1) !== status) window.__fileStatuses.push(status);
        })).observe(document.body, { subtree: true, attributes: true, childList: true, characterData: true });
      });
    }, { session: SESSION, user: USER });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== ORIGIN) return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
      const endpoint = url.pathname;
      if (endpoint.startsWith('/api/')) {
        if (/\/files\/.*\/(preview|download)$/.test(endpoint)) {
          fileRequests.push({ endpoint, session: route.request().headers()['x-session-id'] });
          if (endpoint.includes('fixture-preview-unavailable')) return route.fulfill({ status: 422, json: { detail: '無法轉換預覽，請下載原檔。<img src=x onerror=window.fileInjected=3>' } });
          if (endpoint.endsWith('/preview')) return route.fulfill({ contentType: 'application/pdf', headers: { 'Content-Disposition': 'inline; filename="original.docx"' }, body: PDF });
          return route.fulfill({ contentType: 'application/octet-stream', headers: { 'Content-Disposition': "attachment; filename*=UTF-8''%E5%81%87%E6%9C%9F%E6%95%B4%E7%90%86.docx" }, body: Buffer.from('synthetic-download-bytes') });
        }
        if (endpoint === '/api/chat') {
          const body = route.request().postDataJSON(); chatRequests.push(body);
          if (body.question.includes('無權')) return route.fulfill({ status: 403, json: { detail: '引用檔案不存在或沒有使用權限' } });
          if (body.question.includes('失敗')) {
            const events = [{ type: 'file_status', status: 'error', message: '無法開啟損壞的檔案。' }, { type: 'error', detail: '無法開啟損壞的檔案，請重新上傳完整版本。' }];
            return route.fulfill({ contentType: 'text/event-stream; charset=utf-8', body: events.map(event => 'data: ' + JSON.stringify(event) + '\n\n').join('') });
          }
          if (body.question.includes('中斷')) {
            const events = [{ type: 'file_status', status: 'generating', message: '正在生成檔案' }];
            return route.fulfill({ contentType: 'text/event-stream; charset=utf-8', body: events.map(event => 'data: ' + JSON.stringify(event) + '\n\n').join('') });
          }
          const next = body.question.includes('繼續');
          const artifacts = next ? [{ ...ARTIFACTS[0], id: 'fixture-docx-v2', version: 2, parent_id: ARTIFACTS[0].id }] : ARTIFACTS;
          const answer = next ? '已基於上一版本完成指定修改。' : '已完成，檔案如下。';
          conversation.messages.push({ content: body.question, is_user: true, timestamp: new Date().toISOString() },
            { content: answer, is_user: false, timestamp: new Date().toISOString(), artifacts, file_task_id: next ? 'fixture-task-2' : 'fixture-task-1', citation_map: [] });
          const events = ['reading', 'analyzing', next ? 'editing' : 'generating', 'checking'].map(status => ({ type: 'file_status', status }));
          events.push({ type: 'artifacts', artifacts }, { type: 'content', text: answer }, { type: 'done', artifacts, file_task_id: next ? 'fixture-task-2' : 'fixture-task-1', citation_map: [] });
          return route.fulfill({ contentType: 'text/event-stream; charset=utf-8', body: events.map(event => 'data: ' + JSON.stringify(event) + '\n\n').join('') });
        }
        const payload = endpoint === '/api/session' ? { code: 200, data: { user_info: USER, current_conversation: conversation } }
          : endpoint === '/api/files' ? { code: 200, data: { files: [] } }
          : endpoint === '/api/conversations' ? { code: 200, data: { conversations: [{ id: conversation.id, title: '檔案測試', message_count: conversation.messages.length }] } }
          : endpoint === `/api/conversation/${conversation.id}/switch` ? { code: 200, data: { messages: conversation.messages } }
          : endpoint === '/api/skills' ? { code: 200, data: { skills: [] } }
          : { code: 404, data: {} };
        return route.fulfill({ json: payload });
      }
      if (endpoint === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(ROOT, 'index.html')) });
      const asset = path.resolve(ROOT, decodeURIComponent(endpoint.replace(/^\/static\//, '')));
      if (!asset.startsWith(ROOT + path.sep) || !fs.existsSync(asset) || !fs.statSync(asset).isFile()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ contentType: { '.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png', '.woff2': 'font/woff2' }[path.extname(asset)] || 'application/octet-stream', body: fs.readFileSync(asset) });
    });
    await page.goto(ORIGIN);
    await page.waitForFunction(() => typeof marked !== 'undefined' && startupLoadState?.session === 'file-agent-ui-fixture');
    async function ask(text) {
      await page.locator('#ai-text-input').fill(text);
      const received = page.waitForResponse(response => new URL(response.url()).pathname === '/api/chat');
      await page.locator('#ai-send-btn').click();
      await received;
      await page.waitForFunction(() => !isLoading);
      return page.locator('.message-row.ai').last();
    }
    await page.evaluate(() => addFileTag('校曆.pdf', 'fixture-source-pdf', 100));
    if (!await page.locator('#ai-search-btn').evaluate(element => element.classList.contains('active'))) await page.locator('#ai-search-btn').click();
    const row = await ask('整理全部假期並生成 Word 和 Excel');
    assert.deepEqual(chatRequests[0].file_ids, ['fixture-source-pdf']);
    assert.equal(chatRequests[0].file_references, '校曆.pdf');
    assert.equal(await row.locator('.message-artifact').count(), 2);
    assert.match(await row.locator('.message-artifact-meta').first().textContent(), /DOCX.*版本 1/);
    assert.equal(await row.locator('.file-task-status:visible, .retrieval-status:visible, .web-status:visible').count(), 0);
    assert.equal(await page.locator('#file-tags-container .file-tag').count(), 0);
    results.push('original chat reference sends verified-ID field plus legacy filename; SSE renders two output cards and clears completed status');
    await row.getByRole('button', { name: '預覽 假期整理.docx', exact: true }).click();
    await page.locator('.file-preview-iframe').waitFor();
    assert.equal(await page.locator('.file-preview-iframe').getAttribute('title'), 'original.docx PDF 預覽');
    await page.evaluate(() => closePreviewModal());
    const downloadEvent = page.waitForEvent('download');
    await row.getByRole('button', { name: '下載 假期整理.docx', exact: true }).click();
    const download = await downloadEvent;
    assert.equal(download.suggestedFilename(), '假期整理.docx');
    assert.equal(fs.readFileSync(await download.path()).toString(), 'synthetic-download-bytes');
    assert.ok(fileRequests.every(request => request.session === SESSION));
    results.push('existing authenticated preview renders converted PDF despite original Office filename; existing download preserves actual response bytes and filename');
    const second = await ask('繼續把第一段改成新的內容');
    assert.deepEqual(chatRequests[1].file_ids, []);
    assert.match(await second.locator('.message-artifact-meta').textContent(), /版本 2/);
    await page.waitForFunction(() => JSON.parse(sessionStorage.getItem('houkong.startup.v1:file-agent-ui-fixture'))?.currentConversation?.messages?.at(-1)?.artifacts?.[0]?.version === 2);
    await page.evaluate(() => switchToConversation('file-agent-fixture-conversation'));
    await page.waitForFunction(() => currentConversation?.at(-1)?.artifacts?.[0]?.version === 2);
    assert.equal(await page.locator('.message-artifact').count(), 3);
    results.push('follow-up without re-upload renders new version; server history and per-session startup cache retain artifact metadata');
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.reload();
      await page.locator('.message-artifact').first().waitFor();
      assert.equal(await page.locator('.message-artifact').count(), 3);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      for (const dark of [false, true]) {
        await page.evaluate(value => { document.documentElement.classList.toggle('dark', value); document.documentElement.dataset.theme = value ? 'dark' : 'light'; }, dark);
        assert.equal(await page.locator('.message-artifact').last().getByRole('button', { name: /預覽/ }).isVisible(), true);
        await page.locator('.message-artifact').last().scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(OUTPUT, `artifacts-${width}-${dark ? 'dark' : 'light'}.png`) });
      }
    }
    results.push('artifact cards survive reload at 1440/390/320 in light and dark themes without horizontal overflow');
    await page.evaluate(() => {
      addMessage('安全邊界測試', false, { artifacts: [
        { id: '../../private', filename: '不可見.docx' }, { id: 'https://bad.example', filename: '不可見.xlsx' },
        { id: 'safe-fixture', filename: '<img src=x onerror=window.fileInjected=1>.pdf', description: '<script>window.fileInjected=2</script>' },
        { id: 'safe-fixture', filename: '重複.pdf' }
      ] });
    });
    const safe = page.locator('.message-row.ai').last();
    assert.equal(await safe.locator('.message-artifact').count(), 1);
    assert.equal(await safe.locator('img, script, a[href^="javascript:"]').count(), 0);
    assert.equal(await page.evaluate(() => window.fileInjected), undefined);
    assert.match(await safe.textContent(), /<img/);
    await page.evaluate(() => addMessage('', false, { artifacts: [{ id: 'artifact-only', filename: '僅檔案回覆.pdf', file_size: 20 }] }));
    const interrupted = await ask('中斷：生成文件時流中止');
    assert.equal(await page.locator('.message-artifact[data-file-id="artifact-only"]').count(), 1);
    assert.equal(await interrupted.locator('.message-artifact').count(), 0);
    assert.match(await interrupted.locator('.file-task-status').textContent(), /處理中斷/);
    assert.equal(await interrupted.locator('.file-task-status').isVisible(), true);
    assert.match(await interrupted.locator('.message-bubble').textContent(), /中断/);
    results.push('unsafe IDs and duplicate artifacts are rejected, names/descriptions cannot inject HTML; interrupted tasks show explicit failure with no false output');
    const failed = await ask('失敗：讀取損壞的文件');
    assert.equal(await failed.locator('.message-bubble').isVisible(), true);
    assert.match(await failed.locator('.message-bubble').textContent(), /損壞的檔案/);
    assert.equal(await failed.locator('.message-artifact').count(), 0);
    const forbidden = await ask('無權：使用沒有權限的檔案');
    assert.match(await forbidden.locator('.message-bubble').textContent(), /沒有使用權限/);
    assert.equal(await forbidden.locator('.retrieval-status:visible').count(), 0);
    await page.evaluate(() => addMessage('預覽轉換測試', false, { artifacts: [{ id: 'fixture-preview-unavailable', filename: '預覽失敗.docx' }] }));
    await page.locator('.message-row.ai').last().getByRole('button', { name: '預覽 預覽失敗.docx' }).click();
    await page.locator('.file-preview-error-msg').waitFor();
    assert.match(await page.locator('.file-preview-error-msg').textContent(), /請下載原檔/);
    assert.equal(await page.locator('#file-preview-body img').count(), 0);
    assert.equal(await page.evaluate(() => window.fileInjected), undefined);
    await page.evaluate(() => closePreviewModal());
    results.push('artifact-only replies remain visible after later turns; explicit tool errors keep an actionable message and no downloadable result');
    results.push('permission HTTP errors and failed Office conversion show the server explanation safely');
    // Exercise each real status renderer independently of same-frame buffered fixture delivery.
    const statusLabels = await page.evaluate(() => {
      const node = document.createElement('div');
      return ['reading', 'analyzing', 'generating', 'editing', 'checking', 'validating', 'complete'].map(status => {
        renderFileTaskStatus(node, { status }); return { status, hidden: node.hidden, text: node.textContent };
      });
    });
    assert.ok(statusLabels.slice(0, -1).every(state => !state.hidden && state.text.length > 0));
    assert.equal(statusLabels.at(-1).hidden, true);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(OUTPUT, 'results.json'), JSON.stringify({ passed: true, results, page_errors: errors }, null, 2));
    console.log(JSON.stringify({ passed: true, results, page_errors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
