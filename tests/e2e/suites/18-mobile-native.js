// The phone-behaviour rules for the interface around the page: no tap flash,
// no tap delay, hover styles only where there is a hover, press feedback on
// every control, no scroll chaining, no iOS input zoom, safe-area padding.
// The rendered page (#page-content) is deliberately left alone, and a check
// proves it. What a script cannot prove (a real notch, the keyboard, rubber
// banding, tap latency) is listed in docs/design/validation-suite.md.
async function open(ctx, mobile) {
  const context = await ctx.browser.newContext(mobile
    ? { viewport: { width: 390, height: 800 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 }
    : { viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  page.on('pageerror', e => ctx.check(false, `uncaught page error: ${e.message}`));
  await page.goto(ctx.base + '/__stats');
  await ctx.scenario(page, { chrome: null });
  await page.goto(ctx.base + '/?data=demo');
  await page.waitForFunction(() => typeof navigateTo === 'function');
  await page.waitForTimeout(500);
  page._ctx = context;
  return page;
}
const css = (p, sel, prop) => p.evaluate(([s, pr]) => getComputedStyle(document.querySelector(s)).getPropertyValue(pr), [sel, prop]);

module.exports = async ctx => {
  const phone = await open(ctx, true);

  // ---- the document ---------------------------------------------------
  const meta = await phone.evaluate(() => ({
    viewport: document.querySelector('meta[name="viewport"]').content,
    theme: document.querySelector('meta[name="theme-color"]')?.content,
  }));
  ctx.check(/viewport-fit=cover/.test(meta.viewport), 'the page extends under the notch (viewport-fit=cover)');
  ctx.check(!/user-scalable|maximum-scale/.test(meta.viewport), 'and zoom is never disabled');
  ctx.check(meta.theme === '#1c1c1c', 'the browser bar takes the top bar colour (theme-color)');

  // ---- the phone layout is intact ---------------------------------------
  const lay = await phone.evaluate(() => {
    const r = s => document.querySelector(s).getBoundingClientRect();
    const go = r('#btn-go'), nav = r('#nav-bar'), brand = r('.brand');
    const top = document.elementFromPoint(go.left + go.width / 2, go.top + go.height / 2);
    return { navBelowBrand: nav.top >= brand.bottom - 1, navFull: nav.width > 300, goReachable: !!top && (top.id === 'btn-go' || !!top.closest('#btn-go')),
             addrWidth: r('#address-bar').width };
  });
  ctx.check(lay.navBelowBrand && lay.navFull, 'on a phone the navigation row sits on its own line under the title, full width');
  ctx.check(lay.goReachable, 'and nothing overlaps the Go button');
  ctx.check(lay.addrWidth >= 100, `and the address bar has room to type (${Math.round(lay.addrWidth)}px)`);

  // ---- touch ------------------------------------------------------------
  ctx.check(await css(phone, 'html', '-webkit-tap-highlight-color') === 'rgba(0, 0, 0, 0)', 'no grey flash on tap');
  ctx.check(await css(phone, 'html', 'overscroll-behavior') === 'none', 'no pull-to-refresh or page bounce');
  for (const sel of ['#btn-go', '.sidebar-tab', '#ub-logout']) {
    ctx.check(await css(phone, sel, 'touch-action') === 'manipulation', `${sel}: no tap delay (touch-action: manipulation)`);
  }
  ctx.check(await css(phone, '.sidebar-tab', 'user-select') === 'none', 'a long press does not select a tab label');
  ctx.check(await css(phone, '#sidebar-tab-messages', 'user-select') === 'none', 'nor a button label');
  ctx.check(await css(phone, '#chat-log', 'overscroll-behavior') === 'contain' && await css(phone, '#node-list', 'overscroll-behavior') === 'contain',
    'scrolling a list to its end does not drag the page behind it');

  // ---- hover is for hovering --------------------------------------------
  ctx.check(await phone.evaluate(() => !matchMedia('(hover: hover)').matches), 'the phone context reports no hover (so the checks below mean something)');
  const before = await css(phone, '#btn-go', 'background-color');
  await phone.hover('#btn-go');
  await phone.waitForTimeout(150);
  ctx.check(await css(phone, '#btn-go', 'background-color') === before, 'on a phone a hover style does not stick after a tap');

  // ---- no iOS input zoom -------------------------------------------------
  ctx.check(await css(phone, '#address-bar', 'font-size') === '16px', 'the address bar is 16px on a phone (iOS does not zoom into it)');
  ctx.check(await css(phone, '#node-filter', 'font-size') === '16px', 'and so is the node filter');
  const inPage = await phone.evaluate(() => {
    const i = document.createElement('input'); i.style.cssText = ''; document.getElementById('page-content').appendChild(i);
    const fs = getComputedStyle(i).fontSize; i.remove(); return fs;
  });
  const desktop = await open(ctx, false);
  const inPageDesktop = await desktop.evaluate(() => {
    const i = document.createElement('input'); document.getElementById('page-content').appendChild(i);
    const fs = getComputedStyle(i).fontSize; i.remove(); return fs;
  });
  ctx.check(inPage === inPageDesktop, `a field inside the rendered page is left alone (${inPage} on both)`);

  // ---- safe areas ---------------------------------------------------------
  const sheet = await phone.evaluate(() => fetch('/static/css/style.css').then(r => r.text()));
  ctx.check(['top', 'bottom', 'left', 'right'].every(s => sheet.includes(`safe-area-inset-${s}`)), 'the top bar, status bar and sides pad for the notch and home bar');
  ctx.check(await css(phone, '#topbar', 'padding-top') === '6px', 'and add nothing where there is no notch (the bar keeps its own 6px)');
  await phone._ctx.close();

  // ---- desktop keeps its hover and gains press feedback --------------------
  const rest = await css(desktop, '#btn-go', 'background-color');
  await desktop.hover('#btn-go');
  await desktop.waitForTimeout(150);
  const hovered = await css(desktop, '#btn-go', 'background-color');
  ctx.check(hovered !== rest, 'with a mouse the hover style still applies');
  const box = await desktop.locator('#btn-go').boundingBox();
  await desktop.mouse.move(box.x + 5, box.y + 5);
  await desktop.mouse.down();
  await desktop.waitForTimeout(150);
  ctx.check(await css(desktop, '#btn-go', 'background-color') === 'rgb(126, 200, 227)', 'pressing a control shows feedback at once (:active)');
  await desktop.mouse.up();
  ctx.check(await css(desktop, '#address-bar', 'font-size') === '12px', 'desktop sizes are unchanged (address bar still 12px)');
  await desktop._ctx.close();

  // ---- admin and sign-in pages -----------------------------------------------
  const ctxA = await ctx.browser.newContext({ viewport: { width: 390, height: 800 }, isMobile: true, hasTouch: true });
  const a = await ctxA.newPage();
  await a.goto(ctx.base + '/__stats');
  await ctx.scenario(a, {});
  await a.goto(ctx.base + '/admin/users');
  ctx.check(await a.evaluate(() => document.querySelector('meta[name="theme-color"]')?.content) === '#1c1c1c', 'admin pages set the browser bar colour too');
  ctx.check(await css(a, 'input[name="username"]', 'font-size') === '16px', 'admin form fields are 16px on a phone, even with inline sizes');
  await ctxA.close();
};
