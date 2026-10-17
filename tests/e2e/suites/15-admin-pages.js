// The real admin pages in Chromium: every page loads from the genuine
// templates and routes, the navigation reaches all of them, non-admins are
// refused, and the page with a script (Settings) does what
// they say. Reticulum is faked; nothing else is.
const ROUTES = [
  ['/admin/', 'Dashboard'], ['/admin/interfaces', 'Interfaces'], ['/admin/traffic', 'Traffic'],
  ['/admin/cache', 'Cache'], ['/admin/users', 'Users'], ['/admin/sessions', 'Sessions'],
  ['/admin/logs', 'Logs'], ['/admin/audit', 'Audit'], ['/admin/settings', 'Settings'],
  ['/admin/identities', 'Identities'],
];
const HASH = 'ab'.repeat(16);
const wait = (p, fn, arg) => p.waitForFunction(fn, arg, { timeout: 4000 });
const text = (p, sel) => p.locator(sel).innerText();

module.exports = async ctx => {
  // ---- every page loads, cleanly -------------------------------------
  for (const [route, name] of ROUTES) {
    const p = await ctx.openAdmin({}, route);
    await p.waitForLoadState('load'); await p.waitForTimeout(400);   // the Logs page keeps a stream open, so never wait for idle
    ctx.check(p.status === 200, `${name}: the page loads`);
    ctx.check(p.bad.length === 0, `${name}: no failed requests or console errors${p.bad.length ? ' (' + p.bad.join('; ') + ')' : ''}`);
    ctx.check(await p.locator('#admin-topbar .admin-nav a').count() >= 10, `${name}: the navigation is there`);
  }

  // ---- the navigation reaches every page -----------------------------
  let p = await ctx.openAdmin({}, '/admin/');
  const links = await p.$$eval('#admin-topbar .admin-nav a', as => as.map(a => a.getAttribute('href')));
  const codes = await p.evaluate(async ls => Promise.all(ls.map(l => fetch(l).then(r => r.status))), links);
  ctx.check(codes.every(c => c === 200), `every navigation link opens a page (${links.length} links)`);
  await p.click('#admin-topbar .admin-nav a[href="/admin/cache"]');
  await p.waitForURL('**/admin/cache');
  ctx.check(await p.locator('.admin-nav a.active').innerText().then(t => /Cache/.test(t)), 'and the current page is highlighted');

  // ---- who may see them ----------------------------------------------
  for (const [route, name] of ROUTES.filter(r => r[0] !== '/admin/')) {
    p = await ctx.openAdmin({ admin_role: 'user' }, route);
    if (p.status !== 403) ctx.check(false, `${name}: a signed-in non-admin is refused (got ${p.status})`);
  }
  ctx.check(true, 'a signed-in non-admin is refused on every admin page except the dashboard');
  p = await ctx.openAdmin({ admin_role: 'anon' }, '/admin/cache');
  ctx.check(/\/auth\/login/.test(p.url()), 'a visitor who is not signed in is sent to the login page');

  // ---- Settings ------------------------------------------------------
  p = await ctx.openAdmin({}, '/admin/settings');
  await p.fill('#s-site-name', 'Kiosk <b>One</b>');
  await p.fill('#s-prescribed-nodes', `${HASH.toUpperCase()}\nnot-a-hash\n${HASH}`);
  await p.click('#btn-save-settings');
  await wait(p, () => document.getElementById('settings-status').textContent === 'Saved.');
  ctx.check(true, 'Settings: Save reports "Saved."');
  await p.reload();
  ctx.check(await p.inputValue('#s-site-name') === 'Kiosk <b>One</b>', 'Settings: the site name comes back exactly as typed (not interpreted as HTML)');
  ctx.check(await p.inputValue('#s-prescribed-nodes') === HASH, 'Settings: prescribed nodes are cleaned (case folded, junk dropped, duplicates merged)');
  ctx.check(await p.locator('#access-table').isVisible(), 'Settings: the access table is shown');
  await p.selectOption('#s-access-preset', 'public');
  await p.click('#btn-save-settings');
  await wait(p, () => document.getElementById('settings-status').textContent === 'Saved.');
  await p.reload();
  ctx.check(await p.inputValue('#s-access-preset') === 'public', 'Settings: the access preset is kept');
};
