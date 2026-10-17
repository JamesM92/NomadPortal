// Messaging addresses: pasting one into the page address bar opens a new
// message instead of trying to fetch it as a page, and the New Message
// dialog sends what was typed (the server reads the forms) and shows the
// server's explanation when an address is wrong.
const HASH = 'ab'.repeat(16), KEY = 'cd'.repeat(64);
const stats = p => p.evaluate(() => fetch('/__stats').then(r => r.json()));

module.exports = async ctx => {
  const bar = { admins_address_bar: 'enabled', guests_address_bar: 'enabled', admins_messages_panel: true };
  const open = (extra = {}) => ctx.open({ admin: true, ui_extra: bar, ...extra });

  for (const [label, text] of [['lxmf@ address', `lxmf@${HASH}`], ['lxma:// link', `lxma://${HASH}:${KEY}`], ['angle brackets', `<lxmf@${HASH}>`]]) {
    const p = await open();
    await p.fill('#address-bar', text);
    await p.press('#address-bar', 'Enter');
    await p.waitForSelector('#msg-modal:not([hidden])', { timeout: 4000 });
    ctx.check(await p.inputValue('#msg-dest') === text, `a ${label} in the address bar opens a new message with it filled in`);
    ctx.check((await stats(p)).fetches === 0, `and no page fetch is attempted for the ${label}`);
  }

  // an ordinary hash still means "go to that node"
  let p = await open({ poll: [{ micron: '>x', html: '<div class="mu-page"><p>NODE PAGE</p></div>' }] });
  await p.fill('#address-bar', `hash://${HASH}/page/index.mu`);
  await p.press('#address-bar', 'Enter');
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('NODE PAGE'), null, { timeout: 5000 });
  ctx.check(await p.locator('#msg-modal').isHidden(), 'a node address still opens the page, not a message');

  // a signed-out visitor is told why nothing opened
  p = await ctx.open({ admin: false, ui_extra: bar });
  await p.fill('#address-bar', `lxmf@${HASH}`);
  await p.press('#address-bar', 'Enter');
  await p.waitForFunction(() => /Sign in to send messages/.test(document.getElementById('status-text').textContent), null, { timeout: 4000 });
  ctx.check(await p.locator('#msg-modal').isHidden(), 'signed out: a status message explains, and no message dialog opens');

  // sending sends exactly what was typed
  p = await open();
  await p.evaluate(h => openComposeModal(h), `lxmf@${HASH}`);
  await p.fill('#msg-content', 'hello');
  await p.click('#btn-msg-send');
  await p.waitForFunction(() => fetch('/__stats').then(r => r.json()).then(j => j.message_posts.length > 0), null, { timeout: 5000 });
  const posted = (await stats(p)).message_posts[0];
  ctx.check(posted.json && posted.json.dest_hash === `lxmf@${HASH}` && posted.json.content === 'hello', 'the dialog sends the address as typed; the server does the reading');

  // the server's explanation is shown
  p = await open({ message_error: 'The public key does not belong to this address, so it was not used.' });
  await p.evaluate(h => openComposeModal(h), `lxma://${HASH}:${KEY}`);
  await p.fill('#msg-content', 'hello');
  await p.click('#btn-msg-send');
  await p.waitForFunction(() => !document.getElementById('msg-error').hidden, null, { timeout: 5000 });
  ctx.check(/does not belong/.test(await p.locator('#msg-error').textContent()) && await p.locator('#msg-modal').isVisible(),
    'a wrong address shows the server\'s reason and keeps the dialog open');
  ctx.check(!(await p.locator('#btn-msg-send').isDisabled()), 'and the Send button works again');
};
