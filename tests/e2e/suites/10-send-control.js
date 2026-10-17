// Sent messages: Cancel while queued, Retry when failed or cancelled, a
// marker for automatic re-sends, and no buttons once delivered.
const PEER = 'bb'.repeat(16);
const now = Math.floor(Date.now() / 1000);
const msg = (id, state, extra = {}) => ({ id, dest: PEER, content: 'text ' + id, sent_at: now - 5, state, ...extra });
const log = p => p.evaluate(() => document.getElementById('chat-log').innerText);
const actions = p => p.evaluate(() => fetch('/__stats').then(r => r.json()).then(j => j.msg_actions));

const open = async (ctx, sent, extra = {}) => {
  const p = await ctx.open({ admin: true, sent, contacts: [{ hash: PEER, name: 'Bea' }],
    ui_extra: { admins_messages_panel: true }, ...extra });
  await p.click('#sidebar-tab-messages');
  await p.evaluate(h => { refreshChats().then(() => openConversation(h)); }, PEER);
  await p.waitForSelector('#chat-log .chat-bubble', { timeout: 5000 });
  return p;
};
const stateOf = (p, id) => p.evaluate(i => {
  const b = Array.from(document.querySelectorAll('#chat-log .chat-bubble')).find(x => x.innerText.includes('text ' + i));
  return b ? { text: b.querySelector('.chat-meta').innerText, buttons: Array.from(b.querySelectorAll('button.chat-msg-action')).map(x => x.textContent) } : null;
}, id);

module.exports = async ctx => {
  // 1. buttons follow the state
  let p = await open(ctx, [msg('q', 'queued'), msg('f', 'failed'), msg('c', 'cancelled'), msg('d', 'delivered')]);
  let s = await stateOf(p, 'q');
  ctx.check(s.buttons.join() === 'Cancel', `a queued message offers Cancel (${s.buttons})`);
  s = await stateOf(p, 'f');
  ctx.check(s.buttons.join() === 'Retry', `a failed message offers Retry (${s.buttons})`);
  s = await stateOf(p, 'c');
  ctx.check(s.buttons.join() === 'Retry' && /cancelled/.test(s.text), `a cancelled message says so and offers Retry (${s.text.trim()})`);
  s = await stateOf(p, 'd');
  ctx.check(s.buttons.length === 0 && /delivered/.test(s.text), 'a delivered message has no buttons');

  // 2. cancel: sent to the server, then the message shows as cancelled with Retry
  await p.click('#chat-log .chat-bubble:has-text("text q") button[data-action="cancel"]');
  await p.waitForFunction(() => /cancelled/.test(Array.from(document.querySelectorAll('#chat-log .chat-bubble')).find(x => x.innerText.includes('text q'))?.innerText || ''), null, { timeout: 5000 });
  ctx.check((await actions(p)).includes('cancel:q'), 'Cancel asks the server to cancel that message');
  s = await stateOf(p, 'q');
  ctx.check(s.buttons.join() === 'Retry', `afterwards it offers Retry (${s.buttons})`);

  // 3. retry: failed message goes back to queued with Cancel
  await p.click('#chat-log .chat-bubble:has-text("text f") button[data-action="retry"]');
  await p.waitForFunction(() => /queued/.test(Array.from(document.querySelectorAll('#chat-log .chat-bubble')).find(x => x.innerText.includes('text f'))?.innerText || ''), null, { timeout: 5000 });
  ctx.check((await actions(p)).includes('retry:f'), 'Retry asks the server to send it again');
  s = await stateOf(p, 'f');
  ctx.check(s.buttons.join() === 'Cancel', `and it is queued again, with Cancel (${s.buttons})`);

  // 4. a refusal is explained, not swallowed
  p = await open(ctx, [msg('q', 'queued')], { msg_refuse: 'This message is already delivered' });
  await p.click('button[data-action="cancel"]');
  await p.waitForFunction(() => /already delivered/.test(document.getElementById('status-text')?.textContent || ''), null, { timeout: 5000 });
  ctx.check(true, 'when the server refuses, the reason is shown');
  ctx.check(await p.locator('button[data-action="cancel"]').count() === 1 && !(await p.locator('button[data-action="cancel"]').isDisabled()),
    'and the button works again');

  // 4b. a message left with a propagation node is "stored on a relay", not delivered
  p = await open(ctx, [msg('h', 'held')]);
  s = await stateOf(p, 'h');
  ctx.check(/stored on a relay/.test(s.text) && !/delivered/.test(s.text) && s.buttons.length === 0,
    `a held message says it is stored on a relay and offers no buttons (${s.text.trim()})`);

  // 5. automatic re-sends are marked
  p = await open(ctx, [msg('r', 'queued', { resends: 2 })]);
  const marker = await p.evaluate(() => document.querySelector('.chat-resent')?.textContent);
  ctx.check(/2/.test(marker || ''), `a message sent again automatically shows how many times (${marker})`);
};
