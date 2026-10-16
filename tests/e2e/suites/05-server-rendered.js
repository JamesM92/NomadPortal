// A crawler or link preview (no JavaScript) must get the page content,
// title and canonical link in the HTML. With JavaScript, startup must
// not break and the live page must replace the server copy.
const T = 'dd'.repeat(16);

module.exports = async ctx => {
  const bare = await (await ctx.browser.newContext({ javaScriptEnabled: false })).newPage();
  await bare.goto(ctx.base + '/page/about.mu');
  const html = await bare.content();
  ctx.check(/SSR CONTENT HERE/.test(html), 'no JavaScript: the page content is in the HTML');
  ctx.check(/<title>About us · Test Site<\/title>/.test(html), 'no JavaScript: title tag');
  ctx.check(/rel="canonical" href="http:\/\/x\/page\/about.mu"/.test(html), 'no JavaScript: canonical link');
  ctx.check(await bare.locator('#ssr-page').isVisible(), 'no JavaScript: the content is visible to the reader');
  ctx.check(!(await bare.locator('#welcome').isVisible()), 'no JavaScript: the generic welcome text is hidden');

  const p = await (await ctx.browser.newContext()).newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(ctx.base + '/');
  await ctx.scenario(p, { cached: null, delay: 0,
    poll: [{ micron: '>x', html: '<div class="mu-page"><p>LIVE CONTENT</p></div>' }],
    access: { role: 'guest', read_only: true, landing: T, confined: true, trusted: [T] } });
  await p.goto(ctx.base + '/page/about.mu');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('LIVE CONTENT'), null, { timeout: 8000 });
  ctx.check(errors.length === 0, `JavaScript starts cleanly with server-rendered content present (${errors.join('; ') || 'no errors'})`);
  ctx.check(await p.locator('#ssr-page').count() === 0, 'JavaScript replaced the server copy with the live page');
};
