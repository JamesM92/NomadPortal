// No saved copy and the fetch fails: retry quietly, then a plain error
// page with a Retry button. Real answers (401) are never retried.
const URL_ = `hash://${'dd'.repeat(16)}/page/index.mu`;
const ok_ = { micron: '>x', html: '<div class="mu-page"><p>LOADED</p></div>' };
const ERR = { error: 'Link closed before response' };
const shorten = () => { _COLD_RETRY.windowMs = 2600; _COLD_RETRY.gapMs = 400; };

module.exports = async ctx => {
  let p = await ctx.open({ poll: [ERR] }, { afterLoad: shorten });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForSelector('#page-content .page-unavailable', { timeout: 8000 });
  let t = await ctx.text(p);
  ctx.check(/can.t be loaded right now/.test(t), 'friendly page after the retries');
  ctx.check(!/Link closed/.test(t), 'a guest sees no technical text');
  ctx.check(await p.$('#page-content .page-unavailable-retry') !== null, 'Retry button present');
  ctx.check(await p.evaluate(() => document.getElementById('page-refresh').hidden), 'no refresh indicator on a cold miss');
  const n = await ctx.fetches(p);
  ctx.check(n >= 3, `retried quietly before giving up (${n} attempts)`);

  await ctx.scenario(p, { poll: [ok_] });
  await p.click('.page-unavailable-retry');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('LOADED'), null, { timeout: 5000 });
  ctx.check(true, 'Retry loads the page once the network is back');

  p = await ctx.open({ poll: [ERR, ok_] }, { afterLoad: shorten });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('LOADED'), null, { timeout: 6000 });
  ctx.check(!/can.t be loaded/.test(await ctx.text(p)), 'a retry that succeeds never shows the error page');

  p = await ctx.open({ admin: true, poll: [ERR] }, { afterLoad: shorten });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForSelector('#page-content .page-unavailable', { timeout: 8000 });
  ctx.check(/Link closed/.test(await ctx.text(p)), 'an admin also sees the technical reason');

  p = await ctx.open({ fetch_status: 401, poll: [ok_] }, { afterLoad: shorten });
  await p.evaluate(u => { navigateTo(u); }, URL_);
  await p.waitForFunction(() => !document.getElementById('page-error').hidden, null, { timeout: 4000 });
  ctx.check(/Login required/.test(await p.evaluate(() => document.getElementById('page-error').textContent)), 'a 401 is shown as its own message');
  ctx.check(await p.$('#page-content .page-unavailable') === null, 'a 401 does not get the friendly page');
  ctx.check(await ctx.fetches(p) === 1, 'a 401 is not retried');
};
