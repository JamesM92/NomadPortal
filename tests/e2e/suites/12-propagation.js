// Settings > Propagation: choose a node (or go back to automatic), type one
// in, sync on demand and watch it finish, and have the inbox refreshed.
const A = 'aa'.repeat(16), B = 'bb'.repeat(16), MINE = 'cc'.repeat(16);
const now = Math.floor(Date.now() / 1000);
const status = () => ({
  mode: 'auto', node: A, preferred: null, picked: A,
  nodes: [
    { hash: A, hops: 1, first_seen: now - 900, last_seen: now - 120, announce_count: 4, picked: true, preferred: false },
    { hash: B, hops: 3, first_seen: now - 900, last_seen: now - 600, announce_count: 2, picked: false, preferred: false },
  ],
});
const stats = p => p.evaluate(() => fetch('/__stats').then(r => r.json()));
const text = (p, id) => p.evaluate(i => document.getElementById(i).innerText, id);
const open = async ctx => {
  const p = await ctx.open({ admin: true, prop: status() });
  await p.evaluate(() => _openSettingsModal('propagation'));
  await p.waitForFunction(() => document.querySelectorAll('#prop-node-select option').length > 1, null, { timeout: 5000 });
  return p;
};
const options = p => p.evaluate(() => Array.from(document.querySelectorAll('#prop-node-select option')).map(o => ({ v: o.value, t: o.textContent, sel: o.selected })));

module.exports = async ctx => {
  // 1. what the picker shows
  let p = await open(ctx);
  let o = await options(p);
  ctx.check(o.length === 3 && o[0].v === '' && /Automatic/.test(o[0].t) && o[0].t.includes('aaaaaaaa'), `Automatic comes first and names the nearest node (${o[0].t})`);
  ctx.check(/1 hop,/.test(o[1].t) && /3 hops,/.test(o[2].t), 'each node shows its distance and when it was heard');
  ctx.check(o[0].sel, 'Automatic is selected when nothing is pinned');
  let st = await text(p, 'prop-status');
  ctx.check(/Using aaaaaaaa….*chosen automatically/.test(st) && /Not synced yet/.test(st), `status says which node and that it is automatic (${st.replace(/\n/g, ' | ')})`);

  // 2. pick a node from the list
  await p.selectOption('#prop-node-select', B);
  await p.waitForFunction(() => /your choice/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  ctx.check((await stats(p)).prop_posts.join() === B, 'choosing a node tells the server');
  ctx.check((await options(p)).find(x => x.sel).v === B, 'and the list keeps it selected');

  // 3. back to automatic
  await p.selectOption('#prop-node-select', '');
  await p.waitForFunction(() => /chosen automatically/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  ctx.check((await stats(p)).prop_posts.join() === `${B},`, 'choosing Automatic sends an empty node');

  // 4. type one in by hand
  await p.fill('#prop-node-input', MINE);
  await p.click('#btn-prop-use');
  await p.waitForFunction(() => /your choice/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  o = await options(p);
  ctx.check(o.some(x => x.v === MINE && x.sel && /not heard on the network/.test(x.t)), 'a hand-entered node is listed, selected, and marked as not heard');
  ctx.check(await p.inputValue('#prop-node-input') === '', 'and the box is cleared');

  // 5. a bad hash is explained and changes nothing
  await p.fill('#prop-node-input', 'not-a-hash');
  await p.click('#btn-prop-use');
  await p.waitForFunction(() => !document.getElementById('prop-error').hidden, null, { timeout: 5000 });
  ctx.check(/32 hexadecimal/.test(await text(p, 'prop-error')), 'a bad hash shows why');
  ctx.check((await options(p)).find(x => x.sel).v === MINE, 'and the node in use is unchanged');

  // 6. Enter in the box works like the button
  p = await open(ctx);
  await p.fill('#prop-node-input', MINE);
  await p.press('#prop-node-input', 'Enter');
  await p.waitForFunction(() => /your choice/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  ctx.check(true, 'pressing Enter in the box uses the node');

  // 7. sync now: progress, then the result, then the inbox is refreshed
  p = await open(ctx);
  const before = (await stats(p)).received_gets;
  await p.click('#btn-prop-sync');
  await p.waitForFunction(() => /Collecting messages/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  ctx.check(true, 'Sync now shows that messages are being collected');
  await p.waitForFunction(() => /Last collection: 3 new messages, 1 you already had/.test(document.getElementById('prop-status').innerText), null, { timeout: 9000 });
  ctx.check(/Last sync: /.test(await text(p, 'prop-status')), 'it ends with how many messages arrived, and when');
  await p.waitForFunction(b => fetch('/__stats').then(r => r.json()).then(j => j.received_gets > b), before, { timeout: 5000 });
  ctx.check(true, 'and the inbox is refreshed so the new messages show');

  // 8. a refusal is shown and the button comes back
  p = await ctx.open({ admin: true, prop: status(), prop_sync_refuse: 'Please wait a few seconds before syncing again.' });
  await p.evaluate(() => _openSettingsModal('propagation'));
  await p.waitForFunction(() => document.querySelectorAll('#prop-node-select option').length > 1, null, { timeout: 5000 });
  await p.click('#btn-prop-sync');
  await p.waitForFunction(() => !document.getElementById('prop-error').hidden, null, { timeout: 5000 });
  ctx.check(/wait a few seconds/.test(await text(p, 'prop-error')) && !(await p.locator('#btn-prop-sync').isDisabled()),
    'when the sync is refused the reason shows and the button works again');

  // 9. leaving the tab or closing the dialog stops the polling
  p = await open(ctx);
  await p.click('#btn-prop-sync');
  await p.waitForFunction(() => /Collecting messages/.test(document.getElementById('prop-status').innerText), null, { timeout: 5000 });
  await p.evaluate(() => _closeSettingsModal());
  await p.waitForTimeout(800);   // let a status read already on its way land before counting
  const g1 = (await stats(p)).prop_gets;
  await p.waitForTimeout(3500);
  const g2 = (await stats(p)).prop_gets;
  ctx.check(g2 === g1, `closing the dialog stops the status polling (${g1} -> ${g2})`);
};
