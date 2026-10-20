// The interface around the page (not the rendered page itself) against the
// worst realistic data: a 118-character site title, a long display name,
// nodes with no name or odd clocks, a federated email as a chat name,
// right-to-left, 200% zoom, and the admin tables with unbroken 64-character
// usernames. The data comes from chrome_data.py through the mock's dev-only
// ?data= switch, so what is checked here is what the toggle shows.
const HASH = 'ab'.repeat(16);

async function openWorst(ctx, width, extra = {}) {
  const context = await ctx.browser.newContext({ viewport: { width, height: 800 } });
  const page = await context.newPage();
  page.on('pageerror', e => ctx.check(false, `uncaught page error: ${e.message}`));
  await page.goto(ctx.base + '/__stats');
  await ctx.scenario(page, { chrome: null, ...extra });
  await page.goto(ctx.base + '/?data=worst');
  await page.waitForFunction(() => typeof navigateTo === 'function');
  await page.waitForTimeout(700);
  page._ctx = context;
  return page;
}
const sideways = p => p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const rightEdge = (p, sel) => p.evaluate(s => document.querySelector(s).getBoundingClientRect().right, sel);

module.exports = async ctx => {
  // ---- the top bar ---------------------------------------------------
  for (const w of [320, 390, 768, 1280]) {
    const p = await openWorst(ctx, w);
    ctx.check(await sideways(p) <= 0, `top bar @${w}px: the page does not scroll sideways`);
    const vw = await p.evaluate(() => document.documentElement.clientWidth);
    ctx.check(await rightEdge(p, '#ub-logout') <= vw + 1, `top bar @${w}px: Sign out stays on screen`);
    await p._ctx.close();
  }
  let p = await openWorst(ctx, 1280);
  ctx.check(await p.evaluate(() => { const b = document.querySelector('.brand'); return b.scrollWidth > b.clientWidth; }), 'a long site title is cut with an ellipsis');
  ctx.check((await p.getAttribute('.brand', 'title') || '').includes('Emergency Communications Society Portal'), 'and the full title is in its tooltip');
  ctx.check((await p.getAttribute('#ub-name', 'title') || '').includes('Wiśniewska-Kowalczyk'), 'a long display name has its full text in a tooltip');
  ctx.check(await p.evaluate(() => { const n = document.getElementById('ub-name'); return n.getBoundingClientRect().width <= 14 * parseFloat(getComputedStyle(n).fontSize) + 1; }), 'and is capped in width');
  await p.evaluate(() => { document.documentElement.dir = 'rtl'; });
  await p.waitForTimeout(200);
  const vw = await p.evaluate(() => document.documentElement.clientWidth);
  ctx.check(await sideways(p) <= 0 && await p.evaluate(() => document.getElementById('ub-logout').getBoundingClientRect().left) >= -1,
    'right-to-left: nothing is pushed off the left edge either');
  await p.evaluate(() => { document.documentElement.dir = 'ltr'; document.documentElement.style.zoom = '2'; });
  await p.waitForTimeout(200);
  ctx.check(await p.evaluate(() => document.getElementById('ub-logout').getBoundingClientRect().right) <= vw + 1, 'at 200% zoom Sign out is still on screen');
  await p._ctx.close();

  // ---- the title text is never read as markup --------------------------
  p = await openWorst(ctx, 1280);
  await p.evaluate(() => applyUISettings({ app_title: '<img src=x onerror="window.__owned=1">Plain & simple', app_title_html: '' }));
  await p.waitForTimeout(200);
  ctx.check(await p.evaluate(() => document.querySelectorAll('.brand img').length === 0 && !window.__owned), 'a plain-text title is not interpreted as HTML');
  ctx.check(await p.evaluate(() => document.querySelector('.brand').textContent.includes('<img src=x')), 'and shows as the characters typed');

  // ---- ages ------------------------------------------------------------
  const now = Math.floor(Date.now() / 1000);
  const ages = await p.evaluate(n => [n, n + 7200, n - 3, n - 59, n - 125, n - 3 * 3600, n - 5 * 86400, n - 400 * 86400, 0, null].map(formatAge), now);
  ctx.check(JSON.stringify(ages) === JSON.stringify(['just now', 'just now', 'just now', '59s ago', '2m ago', '3h ago', '5d ago', '13mo ago', 'never', 'never']),
    `ages read sensibly, including clocks ahead and never-seen (${ages.join(' | ')})`);

  await p._ctx.close();

  // ---- lists -----------------------------------------------------------
  p = await openWorst(ctx, 1280);
  await p.click('#sidebar-tab-nodes');
  await p.waitForTimeout(300);
  const names = await p.$$eval('#node-list .node-name', els => els.map(e => e.textContent.trim()));
  ctx.check(names.every(n => n.length > 0), 'every node row has a name (a node with none shows the start of its address)');
  ctx.check(names.some(n => /[0-9a-f]{12}…$/.test(n)), 'and that fallback is the first 12 characters');
  ctx.check(!(await p.$$eval('#node-list .node-age', els => els.map(e => e.textContent))).some(t => /-\d|\d{4,}h/.test(t)), 'no row shows a negative or thousands-of-hours age');
  await p.fill('#node-filter', 'caf');
  await p.waitForTimeout(200);
  ctx.check((await p.$$eval('#node-list .node-name', e => e.length)) >= 1, 'filtering works even when a node has no name');
  await p.fill('#node-filter', '');

  await p.click('#sidebar-tab-messages');
  await p.waitForTimeout(400);
  const badge = await p.evaluate(() => {
    const rows = [...document.querySelectorAll('#chat-list-inner .conv-item')];
    const row = rows.find(r => /bartholomew\.fitzgerald@/.test(r.textContent));
    if (!row) return null;
    const rr = row.getBoundingClientRect(), b = row.querySelector('.inbox-badge');
    return b ? { inside: b.getBoundingClientRect().right <= rr.right + 1, time: row.querySelector('.conv-time').getBoundingClientRect().right <= rr.right + 1 } : { inside: true, time: row.querySelector('.conv-time').getBoundingClientRect().right <= rr.right + 1 };
  });
  ctx.check(badge && badge.inside && badge.time, 'a federated email as a chat name wraps and keeps its unread badge and its time inside the row');

  await p._ctx.close();

  // ---- admin tables ------------------------------------------------------
  for (const w of [390, 1280]) {
    const context = await ctx.browser.newContext({ viewport: { width: w, height: 900 } });
    const a = await context.newPage();
    a.on('pageerror', e => ctx.check(false, `uncaught page error: ${e.message}`));
    await a.goto(ctx.base + '/__stats');
    await ctx.scenario(a, {});
    await a.evaluate(() => fetch('/__admin_seed', { method: 'POST' }));
    for (const route of ['/admin/users', '/admin/sessions', '/admin/identities', '/admin/interfaces', '/admin/settings', '/admin/']) {
      await a.goto(ctx.base + route);
      await a.waitForTimeout(250);
      ctx.check(await sideways(a) <= 0, `admin ${route} @${w}px: wide tables scroll inside their card, not the page`);
    }
    await a.goto(ctx.base + '/admin/users');
    if (w === 1280) {
      ctx.check(await a.evaluate(() => {
        const t = document.querySelector('table.admin-table'), c = t.closest('.card').getBoundingClientRect();
        return t.getBoundingClientRect().right <= c.right + 1;
      }), 'the Users table stays inside its card, so Disable and Delete are reachable');
      ctx.check(await a.evaluate(() => [...document.querySelectorAll('table.admin-table td.nowrap')].every(td => { const r = document.createRange(); r.selectNodeContents(td); return r.getBoundingClientRect().height < 20; })), '"Last seen" stays on one line');
    }
    const rows = await a.$$eval('table.admin-table tbody tr td:first-child', els => els.map(e => e.textContent.trim()));
    ctx.check(rows.every(r => r.length > 0), `admin Users @${w}px: every account has something in its name cell (a missing name falls back to email, then id)`);
    await context.close();
  }
};
