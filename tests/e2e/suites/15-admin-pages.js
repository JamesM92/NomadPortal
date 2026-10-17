// The real admin pages in Chromium: every page loads from the genuine
// templates and routes, the navigation reaches all of them, non-admins are
// refused, and the pages with scripts (Settings, Propagation node) do what
// they say. Reticulum is faked; nothing else is.
const ROUTES = [
  ['/admin/', 'Dashboard'], ['/admin/interfaces', 'Interfaces'], ['/admin/traffic', 'Traffic'],
  ['/admin/cache', 'Cache'], ['/admin/users', 'Users'], ['/admin/sessions', 'Sessions'],
  ['/admin/logs', 'Logs'], ['/admin/audit', 'Audit'], ['/admin/settings', 'Settings'],
  ['/admin/identities', 'Identities'], ['/admin/propagation', 'Propagation node'],
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
  ctx.check(links.includes('/admin/propagation'), 'the Node page is in the navigation');
  await p.click('#admin-topbar .admin-nav a[href="/admin/propagation"]');
  await p.waitForURL('**/admin/propagation');
  ctx.check(await p.locator('.admin-nav a.active').innerText().then(t => /Node/.test(t)), 'and the current page is highlighted');

  // ---- who may see them ----------------------------------------------
  for (const [route, name] of ROUTES.filter(r => r[0] !== '/admin/')) {
    p = await ctx.openAdmin({ admin_role: 'user' }, route);
    if (p.status !== 403) ctx.check(false, `${name}: a signed-in non-admin is refused (got ${p.status})`);
  }
  ctx.check(true, 'a signed-in non-admin is refused on every admin page except the dashboard');
  p = await ctx.openAdmin({ admin_role: 'user' }, '/admin/api/propagation');
  ctx.check(p.status === 403, 'and on the propagation API');
  p = await ctx.openAdmin({ admin_role: 'anon' }, '/admin/propagation');
  ctx.check(/\/auth\/login/.test(p.url()), 'a visitor who is not signed in is sent to the login page');

  // ---- Settings ------------------------------------------------------
  p = await ctx.openAdmin({}, '/admin/settings');
  await p.fill('#s-site-name', 'Kiosk <b>One</b>');
  await p.fill('#s-prescribed-nodes', `${HASH.toUpperCase()}\nnot-a-hash\n${HASH}`);
  await p.fill('#s-stamp-cost', '8');
  await p.click('#btn-save-settings');
  await wait(p, () => document.getElementById('settings-status').textContent === 'Saved.');
  ctx.check(true, 'Settings: Save reports "Saved."');
  await p.reload();
  ctx.check(await p.inputValue('#s-site-name') === 'Kiosk <b>One</b>', 'Settings: the site name comes back exactly as typed (not interpreted as HTML)');
  ctx.check(await p.inputValue('#s-prescribed-nodes') === HASH, 'Settings: prescribed nodes are cleaned (case folded, junk dropped, duplicates merged)');
  ctx.check(await p.inputValue('#s-stamp-cost') === '8', 'Settings: the stamp cost is kept');
  ctx.check(await p.locator('#access-table').isVisible(), 'Settings: the access table is shown');
  await p.selectOption('#s-access-preset', 'public');
  await p.click('#btn-save-settings');
  await wait(p, () => document.getElementById('settings-status').textContent === 'Saved.');
  await p.reload();
  ctx.check(await p.inputValue('#s-access-preset') === 'public', 'Settings: the access preset is kept');

  // ---- Propagation node ----------------------------------------------
  p = await ctx.openAdmin({}, '/admin/propagation');
  await wait(p, () => document.querySelector('#pn-stats .stat-box'));
  ctx.check(/Off/.test(await text(p, '#pn-stats')), 'Node: it starts switched off');
  ctx.check(await p.locator('#pn-enabled').isChecked() === false, 'Node: the box is unticked');
  ctx.check(await p.inputValue('#pn-storage') === '500', 'Node: the storage limit starts at 500 MB');
  ctx.check(await p.locator('#pn-restart').isHidden() && await p.locator('#pn-error').isHidden(), 'Node: no restart notice or error to begin with');
  ctx.check(await p.locator('#pn-nopeers').isVisible(), 'Node: "No peers yet" is shown');

  await p.check('#pn-enabled');
  await p.fill('#pn-name', 'Home <i>node</i>');
  await p.click('#pn-save');
  await wait(p, () => /Running/.test(document.getElementById('pn-stats').innerText));
  ctx.check(true, 'Node: ticking the box and saving starts it');
  const stats = await text(p, '#pn-stats');
  ctx.check(stats.includes('42'.repeat(16)), 'Node: the address is shown');
  ctx.check(/Messages held\s*12/i.test(stats), 'Node: messages held is shown');
  ctx.check(/3\.5 MB of 500\.0 MB/.test(stats), 'Node: storage used and the limit are shown');
  ctx.check(/Up for\s*2 h 1 m/i.test(stats), 'Node: uptime is shown in plain units');
  ctx.check(await text(p, '#pn-peers').then(t => /Friend/.test(t) && /reachable/.test(t) && /3 in/.test(t)), 'Node: the peer table lists the peer');
  ctx.check(await p.locator('#pn-nopeers').isHidden(), 'Node: "No peers yet" goes away');
  ctx.check(await p.locator('#pn-stats i, #pn-peers i').count() === 0 && (await text(p, '#pn-peers')).includes('<i>x</i>'), 'Node: a peer name containing markup is shown as plain text');
  ctx.check(/Saved/.test(await text(p, '#pn-msg')), 'Node: the page says it saved');

  await p.reload();
  await wait(p, () => /Running/.test(document.getElementById('pn-stats').innerText));
  ctx.check(await p.locator('#pn-enabled').isChecked() && await p.inputValue('#pn-name') === 'Home <i>node</i>', 'Node: after a reload the settings are still there and it is still running');

  await p.fill('#pn-storage', '900');
  await p.click('#pn-save');
  await wait(p, () => /900\.0 MB/.test(document.getElementById('pn-stats').innerText));
  ctx.check(await p.locator('#pn-restart').isHidden(), 'Node: a new storage limit applies at once, with no restart notice');

  await p.fill('#pn-maxpeers', '5');
  await p.click('#pn-save');
  await wait(p, () => !document.getElementById('pn-restart').hidden);
  ctx.check(true, 'Node: changing the peer limit says a restart is needed');

  let posts = 0;
  p.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/admin/api/propagation')) posts++; });
  await p.fill('#pn-storage', '-5');
  await p.click('#pn-save');
  await p.waitForTimeout(300);
  ctx.check(posts === 0, 'Node: the browser itself refuses a storage limit below 1 (nothing is sent)');
  await p.evaluate(() => { document.getElementById('pn-form').noValidate = true; });   // reach the server's own check
  await p.click('#pn-save');
  await wait(p, () => /storage_mb/.test(document.getElementById('pn-msg').textContent));
  ctx.check(/Running/.test(await text(p, '#pn-stats')), 'Node: a bad storage limit is explained and the node carries on');

  await p.fill('#pn-storage', '900');
  await p.check('#pn-onlystatic');
  await p.click('#pn-save');
  await wait(p, () => /static peer/.test(document.getElementById('pn-msg').textContent));
  ctx.check(true, 'Node: "fixed peers only" with no fixed peers is refused with a reason');

  await p.fill('#pn-static', `${HASH}\n${HASH.toUpperCase()}\nnonsense`);
  await p.click('#pn-save');
  await wait(p, () => document.getElementById('pn-msg').textContent === 'Saved.');
  await p.reload();
  await wait(p, () => document.getElementById('pn-static').value !== '');
  ctx.check(await p.inputValue('#pn-static') === HASH, 'Node: fixed peers are cleaned and kept');
  ctx.check(await p.locator('#pn-onlystatic').isChecked(), 'Node: "fixed peers only" is accepted once there is a fixed peer');

  await p.click('#pn-announce');
  await wait(p, () => document.getElementById('pn-msg').textContent === 'Announced.');
  ctx.check(true, 'Node: Announce now works while it is running');
  await p.click('#pn-announce');
  await wait(p, () => /moment ago/.test(document.getElementById('pn-msg').textContent));
  ctx.check(true, 'Node: announcing twice in a row is held back, with a reason');

  await p.uncheck('#pn-enabled');
  await p.click('#pn-save');
  await wait(p, () => /Off/.test(document.getElementById('pn-stats').innerText));
  ctx.check(await p.locator('#pn-stats .stat-box').count() === 1, 'Node: switching it off removes the running figures');
  await p.click('#pn-announce');
  await wait(p, () => /not running/.test(document.getElementById('pn-msg').textContent));
  ctx.check(true, 'Node: Announce now says so when the node is off');

  // ---- a failed start is shown, not hidden ---------------------------
  p = await ctx.openAdmin({}, '/admin/propagation');
  await p.evaluate(async () => {
    await fetch('/__admin_break_propagation', { method: 'POST' });
  });
  await p.check('#pn-enabled');
  await p.click('#pn-save');
  await wait(p, () => !document.getElementById('pn-error').hidden);
  ctx.check(/could not start/i.test(await text(p, '#pn-error')) && /network storage/i.test(await text(p, '#pn-error')), 'Node: when it cannot start, the reason is on the page');
  ctx.check(/Error/.test(await text(p, '#pn-stats')), 'Node: and the state reads Error');
};
