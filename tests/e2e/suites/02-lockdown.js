// The browser enforces nothing itself; it renders the server's access
// snapshot. These check that rendering: confined guests are stopped at
// off-node links, trusted nodes load quietly, unconfined guests get the
// external-site warning.
const T = 'dd'.repeat(16), OFF = 'ee'.repeat(16);
const url = h => `hash://${h}/page/index.mu`;
const page1 = { micron: '>x', html: '<div class="mu-page"><p>PAGE</p></div>' };
const snap = (confined, trusted = [T]) => ({ access: { role: 'guest', read_only: true, landing: T, confined, trusted } });

module.exports = async ctx => {
  const go = async (scn, target) => {
    const p = await ctx.open({ poll: [page1], ...scn }, { acceptDialogs: false });
    const ok = await p.evaluate(u => navigateTo(u), url(target));
    await p.waitForTimeout(300);
    return { ok, dialogs: p.dialogs, text: await ctx.text(p) };
  };
  let r = await go(snap(true), OFF);
  ctx.check(r.ok === false && r.dialogs.join() === 'alert', `confined guest, off-node link: blocked with an alert (${r.dialogs})`);
  r = await go(snap(true), T);
  ctx.check(r.ok === true && r.dialogs.length === 0 && /PAGE/.test(r.text), 'confined guest, trusted node: loads with no dialog');
  r = await go(snap(false), OFF);
  ctx.check(r.dialogs.join() === 'confirm', `unconfined guest, off-node: external-site warning (${r.dialogs})`);
  r = await go(snap(false), T);
  ctx.check(r.ok === true && r.dialogs.length === 0, 'unconfined guest, trusted node: no warning');
  r = await go(snap(true, [T, OFF]), OFF);
  ctx.check(r.ok === true && r.dialogs.length === 0, 'a second prescribed node is reachable when confined');
  r = await go({ ui_fail: true }, OFF);
  ctx.check(r.ok !== true, 'settings unavailable: off-node target is not silently allowed (the warning is shown)');
};
