// A link the server marked off-limits becomes plain text with a tooltip;
// ordinary links stay live.
const T = 'dd'.repeat(16);
const html = '<div class="mu-page"><p><a class="mu-link" href="#offnode"><span class="x">Leave</span></a> and ' +
  '<a class="mu-link" href="/page?url=hash%3A%2F%2F' + T + '%2Fpage%2Fb.mu">Stay</a></p></div>';

module.exports = async ctx => {
  const p = await ctx.open({ poll: [{ micron: '>x', html }],
    access: { role: 'guest', read_only: true, landing: T, confined: true, trusted: [T] } });
  await p.evaluate(u => { navigateTo(u); }, `hash://${T}/page/index.mu`);
  await p.waitForFunction(() => document.querySelector('#page-content .mu-offnode'), null, { timeout: 4000 });
  const r = await p.evaluate(() => {
    const off = document.querySelector('#page-content .mu-offnode');
    return { tag: off.tagName, title: off.title, text: off.textContent,
             hasAnchor: !!document.querySelector('#page-content a[href="#offnode"]'),
             inner: !!off.querySelector('.x'), live: document.querySelectorAll('#page-content a.mu-link').length };
  });
  ctx.check(r.tag === 'SPAN' && !r.hasAnchor, 'the off-limits link is no longer an anchor');
  ctx.check(r.text === 'Leave' && r.inner, 'its text and inner markup are preserved');
  ctx.check(/isn.t available here/.test(r.title), `tooltip present ("${r.title}")`);
  ctx.check(r.live === 1, 'the ordinary link is still a live link');
};
