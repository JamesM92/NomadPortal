// Partials: a fragment of another page loads in place (no navigation),
// refreshes on a timer, reloads by id from a `p:` link, and is bounded.
const T = 'dd'.repeat(16);
const ROOT = `hash://${T}/page/index.mu`;
const part = (path, pid, refresh, extra = '') =>
  `<a class="mu-dynamic" href="/page?url=hash%3A%2F%2F${T}%2Fpage%2F${path}" data-pid="${pid}"` +
  (refresh ? ` data-refresh="${refresh}"` : '') + `>[live]</a>${extra}`;
const page = inner => ({ micron: '>x', html: `<div class="mu-page"><p>${inner}</p></div>` });
const frag = text => ({ micron: '>' + text, html: `<div class="mu-page"><p>${text}</p></div>` });
const access = { access: { role: 'guest', read_only: true, landing: T, confined: true, trusted: [T] } };
const fast = () => { _PARTIAL.minRefreshSec = 1; };
const inBox = (p, sel) => p.evaluate(s => { const b = document.querySelector(s); return b ? b.innerText : null; }, sel);

module.exports = async ctx => {
  // 1. loads in place, replaces the "[live]" link, no navigation
  let p = await ctx.open({ ...access, poll: [page('Top ' + part('news.mu', 'news', 0))],
    pages: { '/page/news.mu': [frag('NEWS V1')] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial') && document.querySelector('.mu-partial').innerText.includes('NEWS V1'), null, { timeout: 5000 });
  ctx.check(/Top/.test(await ctx.text(p)) && /NEWS V1/.test(await inBox(p, '.mu-partial')), 'partial content appears inside the page, in place');
  ctx.check(await p.locator('a.mu-dynamic').count() === 0, 'the "[live]" link is gone');
  ctx.check(/index\.mu/.test(await p.evaluate(() => document.getElementById('page-path').textContent)), 'the visitor was not navigated away');

  // 2. timed refresh swaps the content
  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 1))],
    pages: { '/page/news.mu': [frag('TICK 1'), frag('TICK 2')] } }, { afterLoad: fast });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial') && /TICK 1/.test(document.querySelector('.mu-partial').innerText), null, { timeout: 5000 });
  await p.waitForFunction(() => /TICK 2/.test(document.querySelector('.mu-partial').innerText), null, { timeout: 6000 });
  ctx.check(true, 'a partial with a refresh interval updates itself');

  // 3. a p: link reloads that partial by id, without navigating
  const refreshLink = '<a class="mu-link" href="#partial-refresh:news">Reload</a>';
  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 0) + ' ' + refreshLink)],
    pages: { '/page/news.mu': [frag('OLD NEWS'), frag('FRESH NEWS')] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial') && /OLD NEWS/.test(document.querySelector('.mu-partial').innerText), null, { timeout: 5000 });
  await p.click('a.mu-link');
  await p.waitForFunction(() => /FRESH NEWS/.test(document.querySelector('.mu-partial').innerText), null, { timeout: 5000 });
  ctx.check(true, 'a reload link refreshes the named partial in place');

  // 4. a link inside a partial works like any other link
  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 0))],
    pages: { '/page/news.mu': [{ micron: '>l', html: `<div class="mu-page"><a class="mu-link" href="/page?url=hash%3A%2F%2F${T}%2Fpage%2Fother.mu">Go</a></div>` }],
             '/page/other.mu': [page('OTHER PAGE')] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForSelector('.mu-partial a.mu-link', { timeout: 5000 });
  await p.click('.mu-partial a.mu-link');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OTHER PAGE'), null, { timeout: 5000 });
  ctx.check(true, 'a link inside loaded partial content navigates normally');

  // 5. a partial that fails shows a note; on refresh failure the old content stays
  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 1))],
    pages: { '/page/news.mu': [{ error: 'Link closed' }] } }, { afterLoad: fast });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial-failed'), null, { timeout: 5000 });
  const failedText = await inBox(p, '.mu-partial');
  ctx.check(/couldn.t be loaded/.test(failedText) && !/Link closed/.test(failedText), 'a failed partial shows a plain note with no technical text');

  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 1))],
    pages: { '/page/news.mu': [frag('GOOD'), { error: 'Link closed' }] } }, { afterLoad: fast });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial-stale'), null, { timeout: 7000 });
  ctx.check(/GOOD/.test(await inBox(p, '.mu-partial')), 'when a refresh fails the last good content stays');

  // 6. bounded: third level of nesting is plain text; more than 10 per page are skipped
  const level = (n, next) => ({ micron: '>n', html: `<div class="mu-page"><p>L${n} ${next || ''}</p></div>` });
  p = await ctx.open({ ...access, poll: [page('L0 ' + part('a.mu', 'a', 0))],
    pages: { '/page/a.mu': [level(1, part('b.mu', 'b', 0))], '/page/b.mu': [level(2, part('c.mu', 'c', 0))],
             '/page/c.mu': [level(3)] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => /L2/.test(document.getElementById('page-content').innerText), null, { timeout: 6000 });
  await p.waitForTimeout(500);
  ctx.check(!/L3/.test(await ctx.text(p)), 'nesting stops after two levels');
  ctx.check(await p.locator('.mu-partial-skipped').count() === 1, 'the over-deep partial is shown as plain text, not a link');

  const many = Array.from({ length: 13 }, (_, i) => part('news.mu', 'n' + i, 0)).join(' ');
  p = await ctx.open({ ...access, poll: [page(many)], pages: { '/page/news.mu': [frag('X')] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelectorAll('.mu-partial').length > 0, null, { timeout: 5000 });
  ctx.check(await p.locator('.mu-partial').count() === 10 && await p.locator('.mu-partial-skipped').count() === 3, 'at most 10 partials per page; the rest are plain text');

  // 7. leaving the page stops its timers
  p = await ctx.open({ ...access, poll: [page(part('news.mu', 'news', 1))],
    pages: { '/page/news.mu': [frag('T')], '/page/other.mu': [page('ELSEWHERE')] } }, { afterLoad: fast });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('.mu-partial') && /T/.test(document.querySelector('.mu-partial').innerText), null, { timeout: 5000 });
  await p.evaluate(u => { navigateTo(u); }, `hash://${T}/page/other.mu`);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('ELSEWHERE'), null, { timeout: 5000 });
  const before = (await p.evaluate(() => fetch('/__stats').then(r => r.json()))).path_fetches['/page/news.mu'];
  await p.waitForTimeout(3000);
  const after = (await p.evaluate(() => fetch('/__stats').then(r => r.json()))).path_fetches['/page/news.mu'];
  ctx.check(after === before, `no more partial requests after leaving the page (${before} -> ${after})`);
};
