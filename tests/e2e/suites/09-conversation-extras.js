// NomadNet 1.2.0 conversation extras: Trusted / Untrusted tabs, pinned
// conversations, peer notes. "All" is the default so nothing disappears.
const H = n => String(n).repeat(32).slice(0, 32);
const A = H('a'), B = H('b'), C = H('c'), D = H('d');
const now = Math.floor(Date.now() / 1000);
const msg = (src, text, ago, read = true) => ({ id: src + ago, source: src, content: text, received_at: now - ago, read });
const received = [msg(A, 'hi from A', 40), msg(B, 'hi from B', 30, false), msg(C, 'hi from C', 20), msg(D, 'hi from D', 10, false)];
const contacts = [
  { hash: A, name: 'Alice', trust: 'trusted' },
  { hash: B, name: 'Bob' },                                  // no trust field: an old record -> unknown
  { hash: C, name: 'Carol', trust: 'untrusted', note: 'met at the meetup' },
  { hash: D, name: 'Dave', trust: 'trusted', pinned: true },
];
const names = p => p.evaluate(() => Array.from(document.querySelectorAll('#chat-list-inner .conv-name')).map(e => e.textContent.replace(/\s+/g, ' ').trim()));
const tabText = p => p.evaluate(() => Object.fromEntries(Array.from(document.querySelectorAll('.chat-filter')).map(b => [b.dataset.filter, b.textContent])));
const open = async (extra = {}) => {
  const p = await ctxRef.open({ admin: true, received, contacts,
    ui_extra: { admins_messages_panel: true }, ...extra });
  await p.click('#sidebar-tab-messages');                 // the sidebar opens on Nodes
  await p.evaluate(() => refreshChats());
  await p.waitForFunction(() => document.querySelectorAll('#chat-list-inner .conv-item').length > 0, null, { timeout: 5000 });
  return p;
};
let ctxRef;

module.exports = async ctx => {
  ctxRef = ctx;

  // 1. "All" is the default and lists everyone; pinned first, then newest first
  let p = await open();
  let n = await names(p);
  ctx.check(n.length === 4, 'All lists every conversation by default');
  ctx.check(/Dave/.test(n[0]), `a pinned conversation sorts first (${n[0]})`);
  ctx.check(/Dave.*📌|📌.*Dave/.test(n[0]), 'and carries a pin marker');
  ctx.check(/Carol/.test(n[1]) && /Bob/.test(n[2]) && /Alice/.test(n[3]), `the rest keep newest-first order (${n.join(' | ')})`);

  // 2. tab labels carry counts and unread badges
  let t = await tabText(p);
  ctx.check(t.all.startsWith('All (4)'), `All count (${t.all})`);
  ctx.check(t.trusted.startsWith('Trusted (2)') && /1$/.test(t.trusted), `Trusted count with its unread badge (${t.trusted})`);
  ctx.check(t.untrusted.startsWith('Untrusted (2)') && /1$/.test(t.untrusted), `Untrusted count (unknown + untrusted) with unread (${t.untrusted})`);

  // 3. filters
  await p.click('.chat-filter[data-filter="trusted"]');
  n = await names(p);
  ctx.check(n.length === 2 && n.every(x => /Alice|Dave/.test(x)), `Trusted shows only trusted peers (${n.join(' | ')})`);
  await p.click('.chat-filter[data-filter="untrusted"]');
  n = await names(p);
  ctx.check(n.length === 2 && n.some(x => /Bob/.test(x)) && n.some(x => /Carol/.test(x)), 'Untrusted shows unknown and untrusted peers');
  ctx.check(n.some(x => /📝/.test(x)), 'a peer with a note shows a note marker');
  const tip = await p.evaluate(() => document.querySelector('.conv-note')?.title);
  ctx.check(tip === 'met at the meetup', `the note marker's tooltip shows the note ("${tip}")`);

  // 4. the choice is remembered
  const stored = await p.evaluate(() => localStorage.getItem('chatFilter'));
  ctx.check(stored === 'untrusted', 'the chosen tab is remembered');
  await p.reload();
  await p.waitForFunction(() => typeof refreshChats === 'function');
  await p.click('#sidebar-tab-messages');
  await p.evaluate(() => refreshChats());
  await p.waitForFunction(() => document.querySelectorAll('#chat-list-inner .conv-item').length > 0, null, { timeout: 5000 });
  ctx.check(await p.evaluate(() => document.querySelector('.chat-filter.active').dataset.filter) === 'untrusted', 'and restored on the next visit');

  // 5. empty tab message
  p = await open({ contacts: contacts.map(c => ({ ...c, trust: 'untrusted', pinned: false })) });
  await p.click('.chat-filter[data-filter="trusted"]');
  ctx.check(/No trusted conversations/.test(await ctx.text(p).catch(() => '') + await p.evaluate(() => document.getElementById('chat-list-inner').innerText)), 'an empty tab says so');

  // 6. peer info dialog: edit and save
  p = await open();
  await p.click('#chat-list-inner .conv-item:has-text("Bob")');
  await p.click('#btn-peer-info');
  ctx.check(await p.locator('#peer-modal').isVisible(), 'the peer info dialog opens');
  ctx.check(await p.locator('input[name="peer-trust"][value="unknown"]').isChecked(), 'it shows the current trust level (unknown by default)');
  await p.check('input[name="peer-trust"][value="trusted"]');
  await p.check('#peer-pinned');
  await p.fill('#peer-note', 'my cousin');
  await p.click('#btn-peer-save');
  await p.waitForFunction(() => document.getElementById('peer-modal').hidden, null, { timeout: 4000 });
  const posted = (await p.evaluate(() => fetch('/__stats').then(r => r.json()))).settings_posts;
  ctx.check(posted.length === 1 && posted[0].hash === B && posted[0].trust === 'trusted' && posted[0].pinned === true && posted[0].note === 'my cousin',
    'saving sends the trust level, pin and note for that peer');

  // 7. the list reflects the change
  await p.click('#btn-chat-back');
  await p.waitForSelector('#chat-list-inner .conv-item', { timeout: 4000 });
  n = await names(p);
  ctx.check(n.length === 4 && /Dave/.test(n[0]) && /Bob/.test(n[1]) && /📌/.test(n[1]), `Bob is now pinned too, after the newer pinned Dave (${n[0]} | ${n[1]})`);
  t = await tabText(p);
  ctx.check(t.trusted.startsWith('Trusted (3)'), `Bob moved into Trusted (${t.trusted})`);

  // 8. Escape and Cancel close without saving
  await p.click('#chat-list-inner .conv-item:has-text("Carol")');
  await p.click('#btn-peer-info');
  await p.fill('#peer-note', 'should not be saved');
  await p.keyboard.press('Escape');
  ctx.check(await p.evaluate(() => document.getElementById('peer-modal').hidden), 'Escape closes the dialog');
  ctx.check((await p.evaluate(() => fetch('/__stats').then(r => r.json()))).settings_posts.length === 1, 'and nothing is saved');
};
