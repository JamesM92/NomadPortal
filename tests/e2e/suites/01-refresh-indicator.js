// A page with a saved copy shows at once, refreshes behind it, and
// signals the outcome: spinner -> green dot, or red X with the copy kept.
const URL_ = `hash://${'ab'.repeat(16)}/page/index.mu`;
const cached = (age = 7200) => ({ hash: 'ab'.repeat(16), path: '/page/index.mu', age_s: age,
  micron: '>Old', html: '<div class="mu-page"><p>OLD CONTENT</p></div>' });
const fresh = { micron: '>New', html: '<div class="mu-page"><p>NEW CONTENT</p></div>' };
const ind = page => page.evaluate(() => { const e = document.getElementById('page-refresh');
  return { hidden: e.hidden, cls: e.className, title: e.title }; });
const settled = (page, kind) => page.waitForFunction(k =>
  document.getElementById('page-refresh').className.includes(k), kind, { timeout: 5000 });

module.exports = async ctx => {
  let p = await ctx.open({ cached: cached(), delay: 1.2, poll: [{ micron: '>Old', html: cached().html }] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OLD CONTENT'), null, { timeout: 800 });
  let i = await ind(p);
  ctx.check(/OLD CONTENT/.test(await ctx.text(p)), 'saved copy is visible before the refresh completes');
  ctx.check(!i.hidden && i.cls.includes('refreshing'), 'spinner shows while refreshing');
  ctx.check(/2 h ago/.test(i.title), `tooltip shows the copy's age ("${i.title}")`);
  await settled(p, '-ok');
  ctx.check(true, 'green dot after a successful refresh');

  p = await ctx.open({ cached: cached(), delay: 0.5, poll: [fresh] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await settled(p, '-ok');
  const t = await ctx.text(p);
  ctx.check(/NEW CONTENT/.test(t) && !/OLD CONTENT/.test(t), 'changed content is swapped in');

  p = await ctx.open({ cached: cached(), delay: 0.3, poll: [{ error: 'Link closed before response' }] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await settled(p, '-failed');
  i = await ind(p);
  ctx.check(/OLD CONTENT/.test(await ctx.text(p)), 'saved copy stays on screen after a failed refresh');
  ctx.check(/2 h ago/.test(i.title) && !/Link closed/.test(i.title), `guest tooltip: age, no technical text ("${i.title}")`);

  p = await ctx.open({ admin: true, cached: cached(), delay: 0.3, poll: [{ error: 'Link closed before response' }] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await settled(p, '-failed');
  ctx.check(/Link closed/.test((await ind(p)).title), 'admin tooltip includes the technical reason');

  p = await ctx.open({ cached: null, delay: 0.2, poll: [fresh] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('NEW CONTENT'), null, { timeout: 4000 });
  ctx.check((await ind(p)).hidden, 'no saved copy: no indicator, normal load');

  p = await ctx.open({ cached: cached(), delay: 1.5, poll: [fresh] });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OLD CONTENT'), null, { timeout: 800 });
  await ctx.scenario(p, { cached: null, delay: 0, poll: [{ micron: '>Other', html: '<div class="mu-page"><p>OTHER PAGE</p></div>' }] });
  await p.evaluate(u => { navigateTo(u.replace('index', 'other')); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('OTHER PAGE'), null, { timeout: 4000 });
  await p.waitForTimeout(2200);
  const after = await ctx.text(p);
  ctx.check(/OTHER PAGE/.test(after) && !/NEW CONTENT/.test(after), 'a stale refresh does not repaint after navigating away');
  ctx.check((await ind(p)).hidden, 'indicator cleared for the new page');
};
