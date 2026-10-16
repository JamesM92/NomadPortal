// A saved copy still inside its page's #!c=N window is shown as up to
// date with no network job; Refresh forces a live fetch.
const URL_ = `hash://${'ab'.repeat(16)}/page/index.mu`;
const copy = fresh => ({ hash: 'ab'.repeat(16), path: '/page/index.mu', age_s: 600, fresh,
  micron: '#!c=3600\n>Old', html: '<div class="mu-page"><p>OLD CONTENT</p></div>' });
const live = { micron: '#!c=3600\n>New', html: '<div class="mu-page"><p>NEW CONTENT</p></div>' };
const ind = page => page.evaluate(() => { const e = document.getElementById('page-refresh');
  return { hidden: e.hidden, cls: e.className, title: e.title }; });

module.exports = async ctx => {
  let p = await ctx.open({ cached: copy(true), poll: [live] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OLD CONTENT'), null, { timeout: 2000 });
  await p.waitForTimeout(800);
  let i = await ind(p);
  ctx.check(!i.hidden && i.cls.includes('-ok'), 'a fresh copy shows the green dot at once');
  ctx.check(/Up to date/.test(i.title) && /10 min ago/.test(i.title), `tooltip says up to date and how old ("${i.title}")`);
  ctx.check(/OLD CONTENT/.test(await ctx.text(p)), 'the saved copy stays: no live fetch replaced it');

  // The Refresh button must go to the network even though the copy is fresh.
  await p.click('#btn-refresh-page');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('NEW CONTENT'), null, { timeout: 5000 });
  const stats = await p.evaluate(() => fetch('/__stats').then(r => r.json()));
  ctx.check(stats.refresh_flag === true, 'Refresh sends the force-refresh flag');
  ctx.check(/NEW CONTENT/.test(await ctx.text(p)), 'Refresh brings in the live page');

  // Back/forward style navigation does not force a fetch.
  p = await ctx.open({ cached: copy(true), poll: [live] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OLD CONTENT'), null, { timeout: 2000 });
  await p.evaluate(u => { navigateTo(u, false); }, URL_);
  await p.waitForTimeout(600);
  const s2 = await p.evaluate(() => fetch('/__stats').then(r => r.json()));
  ctx.check(s2.refresh_flag === false, 'a plain revisit does not force a live fetch');
  ctx.check(/OLD CONTENT/.test(await ctx.text(p)), 'a plain revisit keeps showing the fresh copy');

  // A stale copy (not fresh) still refreshes behind itself, as before.
  p = await ctx.open({ cached: copy(false), delay: 0.6, poll: [live] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-refresh').className.includes('refreshing'), null, { timeout: 2000 });
  ctx.check(true, 'a copy past its window shows the spinner while it refreshes');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('NEW CONTENT'), null, { timeout: 5000 });
  ctx.check(true, 'and is replaced by the live page');
};
