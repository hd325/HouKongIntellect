// Use the synthetic browser fixture on localhost:8001. No model or user data.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined)});
  const errors = [];
  const results = [];
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.request().url().startsWith('http://127.0.0.1:8001/') ? route.continue() : route.abort());
    await page.request.post('http://127.0.0.1:8001/fixture/reset');
    await page.goto('http://127.0.0.1:8001/fixture');
    const assertNoClassroomPanel = async () => {
      assert.equal(await page.locator('.classroom-context, .classroom-source').count(), 0);
      assert.doesNotMatch(await page.locator('.message-row.ai').allTextContents().then(rows => rows.join('\n')), /課堂出處|測試課堂|fixture-recording|未復聽原音|看決定結構/);
    };
    await page.locator('#ai-text-input').fill('乙烯使溴水褪色是什么反应？');
    await page.locator('#ai-skill-select').selectOption('chemistry-teacher');
    assert.equal(await page.locator('#welcome-skill-select').inputValue(), 'chemistry-teacher');
    await page.locator('#ai-send-btn').click();
    await page.waitForFunction(() => !isLoading);
    assert.equal(await page.locator('.answer-skill-label').last().isVisible(), true);
    assert.equal(await page.locator('.retrieval-status:visible').count(), 0);
    await assertNoClassroomPanel();
    assert.equal(await page.evaluate(()=>window.fixtureInjection), undefined);
    assert.match(await page.locator('.message-row.ai .message-bubble').last().textContent(), /先觀察結構，再判斷反應類型/);
    await page.locator('#ai-skill-select').selectOption('none');
    assert.equal(await page.locator('.answer-skill-label').last().textContent(), '化學老師');
    await page.locator('#ai-text-input').fill('同题，给结论');
    await page.locator('#ai-send-btn').click();
    await page.waitForFunction(() => !isLoading);
    assert.equal(await page.locator('.answer-skill-label').last().isVisible(), false);
    const requests = await (await page.request.get('http://127.0.0.1:8001/fixture/requests')).json();
    assert.equal(requests.at(-2).skill_id,'chemistry-teacher');
    assert.equal(requests.at(-1).skill_id,null);
    const session = await (await page.request.get('http://127.0.0.1:8001/api/session')).json();
    const teacherMessage = session.data.current_conversation.messages.find(message => message.skill_id === 'chemistry-teacher');
    assert.equal(teacherMessage.skill_context.sources.length, 1);
    const output = path.resolve('work/compact-answer-sources-2026-10-19/teacher-ui-verification');
    fs.mkdirSync(output,{recursive:true});
    for (const width of [1440,390,320]) {
      await page.setViewportSize({width,height:width===1440?1000:844});
      await page.reload();
      await page.locator('.message-row.ai').last().waitFor();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth),true);
      assert.equal(await page.locator('.answer-skill-label').last().isVisible(),false);
      assert.equal(await page.locator('.answer-skill-label').first().textContent(),'化學老師');
      await assertNoClassroomPanel();
      await page.screenshot({path:path.join(output,`${width}.png`)});
      results.push({width,no_horizontal_overflow:true,history_modes_preserved:true,classroom_panel_absent:true});
    }
    await page.locator('#ai-skill-select').selectOption('chemistry-teacher');
    await page.locator('#ai-text-input').fill('無來源：化學平衡怎麼判斷？');
    await page.locator('#ai-send-btn').click();
    await page.waitForFunction(() => !isLoading);
    assert.equal(await page.locator('.answer-skill-label').last().textContent(),'化學老師');
    await assertNoClassroomPanel();
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'results.json'),JSON.stringify({results,page_errors:errors,passed:true},null,2));
    console.log(JSON.stringify({passed:true,viewports:results.map(r=>r.width),page_errors:errors.length}));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
