const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');

(async () => {
  const baseUrl = process.env.BASE_URL || 'http://127.0.0.1:8765/';
  const browser = await chromium.launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
  try {
  const context = await browser.newContext({viewport: {width: 1440, height: 1100}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseUrl, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('.video-card');
  assert.equal(await page.locator('.video-card').count(), 24);
  await fs.mkdir('data', {recursive: true});
  await page.screenshot({path: 'data/ui-desktop.png', fullPage: false});
  await page.locator('#search-input').fill('Grceful');
  await page.locator('#suggestions li').filter({hasText: 'Graceful'}).waitFor({state: 'visible'});
  await page.locator('#search-input').press('ArrowDown');
  await page.locator('#search-input').press('Enter');
  await page.waitForFunction(() => document.querySelector('#results-title').textContent === 'Graceful');
  assert.ok((await page.locator('#result-count').innerText()).includes('1,'));
  await page.locator('#next-page').click();
  await page.waitForFunction(() => document.querySelector('#page-label').textContent.startsWith('Page 2 '));
  assert.equal(await page.locator('.video-card').count(), 24);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#page-label').textContent.startsWith('Page 2 '));
  await page.locator('#map-tab').click();
  await page.locator('#search-input').fill('Supreme Isthmus v2.1');
  await page.locator('#search-form').evaluate(form => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector('#results-title').textContent === 'Supreme Isthmus v2.1');
  assert.equal(await page.locator('.video-card').count(), 24);
  await page.locator('#channel-filter').selectOption({label: 'BrightWorksGaming'});
  await page.waitForFunction(() => document.querySelector('#results').getAttribute('aria-busy') === 'false');
  assert.ok((await page.locator('.video-meta span').allTextContents()).every(name => name === 'BrightWorksGaming'));
  await page.locator('#period-filter').selectOption('30');
  await page.waitForFunction(() => document.querySelector('#results').getAttribute('aria-busy') === 'false');
  await page.goBack();
  await page.waitForFunction(() => document.querySelector('#period-filter').value === 'all');
  // An external metadata string must remain text, never markup.
  const unsafe = '<img src=x onerror=alert(1)>';
  await page.locator('#player-tab').click();
  await page.locator('#search-input').fill(unsafe);
  await page.locator('#search-form').evaluate(form => form.requestSubmit());
  await page.waitForFunction(value => document.querySelector('#results-title').textContent === value, unsafe);
  assert.equal(await page.locator('#results-title img').count(), 0);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto(new URL('?playerName=Graceful', baseUrl).href, {waitUntil: 'domcontentloaded'});
  await page.waitForSelector('.video-card');
  await page.screenshot({path: 'data/ui-mobile.png', fullPage: false});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  // Exercise typing under CPU throttling, excluding initial page setup.
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', {rate: 4});
  await page.evaluate(() => {
    window.typingLongTasks = [];
    window.typingEvents = [];
    new PerformanceObserver(list => window.typingLongTasks.push(...list.getEntries().map(e => e.duration))).observe({type: 'longtask'});
    document.querySelector('#search-input').addEventListener('input', () => {
      const start = performance.now();
      requestAnimationFrame(() => window.typingEvents.push(performance.now() - start));
    });
  });
  await page.locator('#search-input').fill('');
  await page.locator('#search-input').pressSequentially('SentientWaffle', {delay: 15});
  await page.locator('#suggestions li').filter({hasText: 'SentientWaffle'}).waitFor({state: 'visible'});
  const performance = await page.evaluate(() => ({longTasks: window.typingLongTasks, inputToNextFrame: window.typingEvents}));
  assert.deepEqual(errors, []);
  assert.deepEqual(performance.longTasks, [], 'Typing should not create main-thread tasks over 50 ms');

  const retryContext = await browser.newContext();
  await retryContext.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('Blocked', 'SecurityError'); }});
  });
  let failCatalog = true;
  await retryContext.route('**/catalog.*.json', route => {
    if (failCatalog) { failCatalog = false; return route.abort(); }
    return route.continue();
  });
  const retryPage = await retryContext.newPage();
  retryPage.on('pageerror', error => errors.push(error.message));
  await retryPage.goto(baseUrl);
  await retryPage.locator('#retry-load').waitFor({state: 'visible'});
  await retryPage.locator('#retry-load').click();
  await retryPage.waitForSelector('.video-card');
  assert.equal(await retryPage.locator('.video-card').count(), 24);
  await retryPage.locator('#search-input').focus();
  await retryPage.locator('#suggestions li').last().waitFor({state: 'visible'});
  const lastSuggestion = await retryPage.locator('#suggestions li span').last().innerText();
  await retryPage.locator('#search-input').press('ArrowUp');
  await retryPage.locator('#search-input').press('Enter');
  await retryPage.waitForFunction(name => document.querySelector('#results-title').textContent === name, lastSuggestion);
  await retryPage.locator('#search-input').dispatchEvent('compositionstart');
  await retryPage.locator('#search-input').fill('Grceful');
  assert.equal(await retryPage.locator('#suggestions').isHidden(), true);
  await retryPage.locator('#search-input').dispatchEvent('compositionend');
  await retryPage.locator('#suggestions li').filter({hasText: 'Graceful'}).waitFor({state: 'visible'});
  await retryPage.locator('#suggestions li').filter({hasText: 'Graceful'}).click();
  await retryPage.waitForFunction(() => document.querySelector('#results-title').textContent === 'Graceful');
  await retryContext.close();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({browserErrors: errors, performance}, null, 2));
  await fs.writeFile('data/ui-smoke-results.json', JSON.stringify({browserErrors: errors, performance}, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
