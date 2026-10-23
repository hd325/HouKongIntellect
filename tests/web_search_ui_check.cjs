// Production HTML, synthetic accounts and replies only; fixture port 8012.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const output = path.resolve('work/compact-answer-sources-2026-10-19/ui-verification');
  fs.mkdirSync(output, { recursive: true });
  const results = [], errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:8012/') ? route.continue() : route.abort());
    await page.request.post('http://127.0.0.1:8012/fixture/reset');
    await page.goto('http://127.0.0.1:8012/fixture');
    await page.waitForFunction(() => typeof marked !== 'undefined' && startupLoadState?.session === 'web-search-fixture');
    async function ask(text, search = true, think = false) {
      for (const [id, enabled] of [['#ai-search-btn', search], ['#ai-think-btn', think]]) {
        const button = page.locator(id);
        if (await button.evaluate(el => el.classList.contains('active')) !== enabled) await button.click();
      }
      await page.locator('#ai-text-input').fill(text);
      await page.locator('#ai-send-btn').click();
      await page.waitForFunction(() => !isLoading);
      return page.locator('.message-row.ai').last();
    }
    const mixed = await ask('混合：使用網頁及指定文件', true, true);
    assert.equal(await mixed.locator('.answer-sources').count(), 1);
    assert.equal(await mixed.locator('.answer-sources').isVisible(), true);
    assert.equal(await mixed.locator('.answer-sources details').evaluate(el => el.open), false);
    assert.equal(await mixed.evaluate(el => Boolean(el.querySelector('.answer-sources').compareDocumentPosition(el.querySelector('.message-bubble')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
    assert.match(await mixed.locator('.answer-sources summary').textContent(), /網頁 1 個.*檔案 1 個/);
    assert.deepEqual(await mixed.locator('.message-bubble .answer-citation').allTextContents(), ['1', '2']);
    assert.equal(await mixed.locator('.think-section .answer-citation, code .answer-citation').count(), 0);
    assert.equal(await mixed.locator('.answer-skill-label').isVisible(), false);
    assert.equal(await mixed.locator('.retrieval-status:visible, .web-status:visible, .web-sources:visible').count(), 0);
    await mixed.locator('.web-ref').click();
    assert.equal(await mixed.locator('.answer-sources details').evaluate(el => el.open), true);
    assert.equal(await mixed.locator('.answer-source-row').count(), 2);
    assert.doesNotMatch(await mixed.locator('.answer-sources').textContent(), /中文摘要來源|未引用候選/);
    assert.match(await mixed.locator('.answer-sources').textContent(), /已讀取正文.*正文已節錄/s);
    assert.match(await mixed.locator('.answer-sources').textContent(), /搜尋摘要：/);
    assert.equal(await mixed.locator('.answer-sources a').getAttribute('rel'), 'noopener noreferrer');
    assert.equal(await page.locator('.message-bubble img, .message-bubble script, .answer-sources img, .answer-sources script').count(), 0);
    assert.equal(await page.locator('a[href^="javascript:"]').count(), 0);
    assert.equal(await page.evaluate(() => window.fixtureInjection), undefined);
    await page.screenshot({ path: path.join(output, 'desktop-expanded.png') });
    results.push('one compact source bar above answer, unique stable badges and only actual body citations');
    results.push('safe source details, source-location click, snippets labelled honestly, model HTML cannot execute');
    await mixed.locator('.answer-sources summary').click();
    const grouped = await ask('多段文件：核對同一檔案的第一及第三頁', false);
    const groupedTitle = '高二化學下學期預習與課堂綜合練習暨課程重點整理2026-2027學年完整修訂版.pdf';
    const groupedRow = () => page.locator('.message-row.ai').filter({ has: page.locator('.message-bubble', { hasText: '合成分組測試' }) }).first();
    async function checkGroupedFiles(row, width) {
      assert.match(await row.locator('.answer-sources summary').textContent(), /檔案來源（2）/);
      assert.equal(await row.locator('.answer-source-row').count(), 2);
      assert.deepEqual(await row.locator('.message-bubble .answer-citation').allTextContents(), ['1', '2', '3', '4', '2']);
      const files = row.locator('.answer-source-file-row');
      assert.equal(await files.count(), 2);
      assert.deepEqual(await files.first().locator('.answer-source-number').allTextContents(), ['1', '2', '3']);
      assert.deepEqual(await files.last().locator('.answer-source-number').allTextContents(), ['4']);
      assert.deepEqual((await files.first().locator('.answer-source-pages').textContent()).match(/\d+/g), ['1', '3']);
      assert.deepEqual((await files.last().locator('.answer-source-pages').textContent()).match(/\d+/g), ['2']);
      assert.equal(await files.locator('.answer-source-excerpt').count(), 0);
      assert.doesNotMatch(await row.locator('.answer-sources').textContent(), /文件原文|不應顯示的冗長檔案節錄|未引用候選/);
      for (const file of await files.all()) {
        const title = file.locator('.answer-source-title');
        assert.equal(await title.getAttribute('title'), groupedTitle);
        assert.equal(await title.textContent(), groupedTitle);
        assert.equal(await title.evaluate(el => getComputedStyle(el).textOverflow), 'ellipsis');
        assert.equal(await title.evaluate(el => getComputedStyle(el).whiteSpace), 'nowrap');
        assert.equal(await file.locator('button').filter({ hasText: /^查看$/ }).count(), 1);
        const box = await file.boundingBox();
        assert.ok(box && box.height <= (width >= 768 ? 64 : 112), `source row too tall at ${width}: ${box?.height}`);
      }
    }
    assert.equal(await grouped.locator('.answer-sources details').evaluate(el => el.open), false);
    await grouped.locator('.answer-sources summary').click();
    await checkGroupedFiles(grouped, 1440);
    await grouped.locator('.answer-source-file-row').first().locator('.answer-source-title').click();
    await page.locator('#source-view-body').getByText('精確片段：group-a-three-first', { exact: true }).waitFor();
    await page.locator('#source-view-close').click();
    await grouped.locator('.answer-source-file-row').last().locator('button').filter({ hasText: /^查看$/ }).click();
    await page.locator('#source-view-body').getByText('精確片段：group-b-two', { exact: true }).waitFor();
    await page.locator('#source-view-close').click();
    await grouped.locator('.answer-source-file-row').first().locator('.answer-source-number').filter({ hasText: /^2$/ }).click();
    await page.locator('#source-view-body').getByText('精確片段：group-a-one', { exact: true }).waitFor();
    await page.locator('#source-view-close').click();
    await grouped.locator('.message-bubble .answer-citation[data-source-id="2"]').click();
    await page.locator('#source-view-body').getByText('精確片段：group-a-three-second', { exact: true }).waitFor();
    await page.locator('#source-view-close').click();
    const sourceRequests = await (await page.request.get('http://127.0.0.1:8012/fixture/source-requests')).json();
    assert.deepEqual(sourceRequests, ['group-a-three-first', 'group-b-two', 'group-a-one', 'group-a-three-second']);
    results.push('same document groups into one short row with all citation numbers and unique pages; same-name documents stay separate');
    results.push('filename hover reveals full name; filename, view action and each exact citation open the corresponding original excerpt');
    for (const [search, think] of [[false, false], [false, true], [true, false], [true, true]]) {
      const row = await ask(`模式 search=${search} think=${think}`, search, think);
      assert.equal(await row.locator('.think-section').count(), think ? 1 : 0);
      assert.equal(await row.locator('.answer-sources').isVisible(), search);
      assert.equal(await row.locator('.answer-citation').count(), search ? 1 : 0);
      assert.equal(await row.locator('.retrieval-status:visible').count(), 0);
    }
    results.push('all search/thinking combinations and skipped local retrieval have no false file warning');
    const partial = await ask('摘要：部分頁面未能讀取');
    assert.match(await partial.locator('.answer-source-state').textContent(), /部分為搜尋摘要/);
    assert.equal(await partial.locator('.web-status:visible').count(), 0);
    await partial.locator('.answer-sources summary').click();
    assert.match(await partial.locator('.answer-sources').textContent(), /僅搜尋摘要/);
    results.push('partial fetch is a compact source badge with per-source evidence state');
    for (const query of ['失败：搜索服務錯誤', '无结果：未找到資料', '直链失败：https://www.example.org/unavailable', '处理中断：等待抓取時斷線']) {
      const row = await ask(query);
      assert.equal(await row.locator('.answer-source-note').isVisible(), true);
      assert.equal(await row.locator('.answer-citation').count(), 0);
      assert.equal(await row.locator('.web-status:visible, .retrieval-status:visible').count(), 0);
    }
    results.push('all-failed/no-results/unreadable URL/interrupted search retain a short explicit message');
    await page.evaluate(() => switchToConversation('web-fixture-conversation'));
    const first = page.locator('.message-row.ai').first();
    assert.deepEqual(await first.locator('.message-bubble .answer-citation').allTextContents(), ['1', '2']);
    assert.equal(await first.locator('.answer-source-row').count(), 2);
    assert.equal(await groupedRow().locator('.answer-sources details').evaluate(el => el.open), false);
    await groupedRow().locator('.answer-sources summary').click();
    await checkGroupedFiles(groupedRow(), 1440);
    await page.waitForFunction(() => {
      const state = JSON.parse(sessionStorage.getItem('houkong.startup.v1:web-search-fixture'));
      return state?.currentConversation?.messages?.some(message => message.rag_status?.status === 'skipped');
    });
    results.push('conversation switch and session cache preserve source metadata and skipped status');
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
      for (const dark of [false, true]) {
        await page.reload();
        await page.locator('.message-row.ai .answer-citation').first().waitFor();
        await page.evaluate(value => { document.documentElement.classList.toggle('dark', value); document.documentElement.dataset.theme = value ? 'dark' : 'light'; }, dark);
        await page.addStyleTag({ content: '* { animation-duration: 0s !important; transition-duration: 0s !important; }' });
        const firstRow = page.locator('.message-row.ai').first();
        assert.deepEqual(await firstRow.locator('.message-bubble .answer-citation').allTextContents(), ['1', '2']);
        assert.equal(await firstRow.locator('.answer-sources').count(), 1);
        assert.equal(await groupedRow().locator('.answer-sources details').evaluate(el => el.open), false);
        await groupedRow().locator('.answer-sources summary').click();
        await checkGroupedFiles(groupedRow(), width);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await groupedRow().scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(output, `${width}-${dark ? 'dark' : 'light'}.png`) });
      }
    }
    results.push('history restores collapsed grouped files and unique source numbers in light/dark themes at 1440/390/320 without overflow');
    // Fresh contexts and local DOM fixtures cover uncommon historical metadata without
    // changing the shared server's messages, requests or source-request log.
    for (const width of [320, 390, 601, 768, 1440]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 } });
      try {
        const edge = await context.newPage();
        edge.on('pageerror', error => errors.push(error.message));
        await edge.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:8012/') && route.request().method() === 'GET' ? route.continue() : route.abort());
        const originalRequests = [];
        await edge.route('**/api/knowledge/chunks/review-usable', route => {
          originalRequests.push(route.request().url());
          return route.fulfill({ json: { code: 200, data: { document_id: 'review-recovery', filename: '缺少片段定位.pdf',
            page: 3, file_type: 'txt', content: '獨立測試的可用原文片段', context_before: [], context_after: [] } } });
        });
        await edge.goto('http://127.0.0.1:8012/fixture');
        await edge.waitForFunction(() => typeof marked !== 'undefined' && startupLoadState?.session === 'web-search-fixture');
        await edge.evaluate(() => {
          addMessage('八處同檔引用邊界[1][2][3][4][5][6][7][8]。', false, { citation_map: Array.from({ length: 8 }, (_, index) => ({
            index: index + 1, document_id: 'review-eight', filename: '八處引用檢查完整長檔案名稱之歷史資料2026學年校曆原文.pdf',
            page: index + 1, chunk_id: `review-${index}`
          })) });
          addMessage('舊資料沒有文件ID[1][2]。', false, { citation_map: [
            { index: 1, filename: '相同檔案名稱.pdf', page: 1, chunk_id: 'review-legacy-one' },
            { index: 2, filename: '相同檔案名稱.pdf', page: 2, chunk_id: 'review-legacy-two' }
          ] });
          addMessage('首處缺少片段與頁碼[1]，第二處可定位[2]。', false, { citation_map: [
            { index: 1, document_id: 'review-recovery', filename: '缺少片段定位.pdf' },
            { index: 2, document_id: 'review-recovery', filename: '缺少片段定位.pdf', page: 3, chunk_id: 'review-usable' }
          ] });
        });
        const rowByText = text => edge.locator('.message-row.ai').filter({ has: edge.locator('.message-bubble', { hasText: text }) }).last();
        const eight = rowByText('八處同檔引用邊界');
        assert.equal(await eight.locator('details').evaluate(el => el.open), false);
        await eight.locator('summary').click();
        const eightFile = eight.locator('.answer-source-file-row');
        assert.equal(await eightFile.count(), 1);
        assert.deepEqual(await eightFile.locator('.answer-source-number').allTextContents(), ['1', '2', '3', '4', '5', '6', '7', '8']);
        assert.equal(await eightFile.locator('.answer-source-pages').textContent(), '第 1–8 頁');
        assert.equal(await eightFile.locator('.answer-source-pages').getAttribute('title'), '第 1、2、3、4、5、6、7、8 頁');
        assert.ok((await eightFile.locator('.answer-source-title').boundingBox()).width >= 140, `filename squeezed at ${width}`);
        assert.equal(await eightFile.locator('.answer-source-pages').evaluate(el => el.scrollWidth <= el.clientWidth + 1 && el.scrollHeight <= el.clientHeight + 1), true);
        assert.ok((await eightFile.boundingBox()).height <= 120, `eight-citation row too tall at ${width}`);
        const legacy = rowByText('舊資料沒有文件ID');
        assert.match(await legacy.locator('summary').textContent(), /檔案來源（2）/);
        assert.equal(await legacy.locator('.answer-source-file-row').count(), 2);
        const recovery = rowByText('首處缺少片段與頁碼');
        await recovery.locator('summary').click();
        const recoveryFile = recovery.locator('.answer-source-file-row');
        assert.equal(await recoveryFile.count(), 1);
        assert.equal(await recoveryFile.locator('.answer-source-number').first().isDisabled(), true);
        assert.equal(await recoveryFile.locator('.answer-source-number').last().isDisabled(), false);
        assert.equal(await recoveryFile.locator('.answer-source-title').getAttribute('data-chunk'), 'review-usable');
        assert.equal(await recoveryFile.locator('.answer-source-action').getAttribute('data-chunk'), 'review-usable');
        assert.equal(await recoveryFile.locator('.answer-source-pages').textContent(), '第 3 頁');
        await recoveryFile.locator('.answer-source-action').click();
        await edge.locator('#source-view-body').getByText('獨立測試的可用原文片段', { exact: true }).waitFor();
        await edge.locator('#source-view-close').click();
        assert.equal(originalRequests.length, 1);
        assert.equal(await edge.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await eight.scrollIntoViewIfNeeded();
        await edge.screenshot({ path: path.join(output, `edge-${width}.png`) });
      } finally { await context.close(); }
    }
    results.push('eight citations keep readable filenames and complete page ranges at 320/390/601/768/1440; missing IDs stay distinct and missing chunks use the next valid original');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ passed: true, results, page_errors: errors }, null, 2));
    console.log(JSON.stringify({ passed: true, results, page_errors: errors.length }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
