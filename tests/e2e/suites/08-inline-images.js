// NomadNet inline images: fetched through /api/media a couple at a time,
// alt text as placeholder and fallback, busy-retry, no leaks across pages.
const T = 'dd'.repeat(16);
const ROOT = `hash://${T}/page/index.mu`;
const access = { access: { role: 'guest', read_only: true, landing: T, confined: true, trusted: [T] } };
const fig = (name, alt = name) =>
  `<figure class="mu-image"><img class="mu-image-el" alt="${alt}" data-media-hash="${T}" data-media-path="/media/${name}.webp" style="max-width:100%">` +
  `<figcaption class="mu-image-alt">${alt}</figcaption></figure>`;
const page = inner => ({ micron: '>x', html: `<div class="mu-page">${inner}</div>` });
const quick = () => { _IMG.retryMs = 150; };
const stats = p => p.evaluate(() => fetch('/__stats').then(r => r.json()));
const state = (p, name) => p.evaluate(n => {
  const img = document.querySelector(`img[data-media-path="/media/${n}.webp"]`);
  if (!img) return null;
  const f = img.closest('figure');
  return { st: img.dataset.mediaState, src: (img.getAttribute('src') || '').slice(0, 22), w: img.naturalWidth,
           loaded: f.classList.contains('mu-image-loaded'), failed: f.classList.contains('mu-image-failed'),
           capVisible: getComputedStyle(f.querySelector('figcaption')).display !== 'none',
           imgVisible: getComputedStyle(img).display !== 'none' };
}, name);
const loaded = (p, name) => p.waitForFunction(n =>
  document.querySelector(`img[data-media-path="/media/${n}.webp"]`)?.dataset.mediaState === 'loaded', name, { timeout: 8000 });

module.exports = async ctx => {
  // 1. an image loads, becomes visible, and its caption steps aside
  let p = await ctx.open({ ...access, poll: [page('Hello ' + fig('a', 'The logo'))] });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await loaded(p, 'a');
  let st = await state(p, 'a');
  ctx.check(/^data:image\/webp/.test(st.src) && st.w === 1, 'the image is drawn from a data: URL and decodes');
  ctx.check(st.imgVisible && !st.capVisible, 'the image shows and the alt-text caption hides');

  // 2. before it arrives the alt text is what the reader sees
  p = await ctx.open({ ...access, media_delay: 1.0, poll: [page(fig('a', 'The logo'))] });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForSelector('figure.mu-image', { timeout: 4000 });
  st = await state(p, 'a');
  ctx.check(st.capVisible && !st.imgVisible, 'while loading, only the alt text shows (no broken-image icon)');

  // 3. "busy" from the server is retried and then succeeds
  p = await ctx.open({ ...access, media: { '/media/a.webp': { status: 200, busy_times: 2 } }, poll: [page(fig('a'))] }, { afterLoad: quick });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await loaded(p, 'a');
  const s3 = await stats(p);
  ctx.check(s3.media_calls['/media/a.webp'] === 3, `busy answers are retried (${s3.media_calls['/media/a.webp']} requests)`);

  // 4. not found: the alt text stays, flagged as failed, nothing broken shows
  p = await ctx.open({ ...access, media: { '/media/gone.webp': { status: 404 } }, poll: [page(fig('gone', 'Missing logo'))] });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelector('figure.mu-image-failed'), null, { timeout: 5000 });
  st = await state(p, 'gone');
  ctx.check(st.capVisible && !st.imgVisible && st.failed, 'a missing image leaves its alt text and no broken image');
  ctx.check((await stats(p)).media_calls['/media/gone.webp'] === 1, 'a 404 is not retried');

  // 5. never more than two at once
  const many = Array.from({ length: 6 }, (_, i) => fig('i' + i)).join('');
  p = await ctx.open({ ...access, media_delay: 0.4, poll: [page(many)] });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForFunction(() => document.querySelectorAll('figure.mu-image-loaded').length === 6, null, { timeout: 12000 });
  const s5 = await stats(p);
  ctx.check(s5.media_max_inflight <= 2, `at most 2 images in flight at once (saw ${s5.media_max_inflight})`);

  // 6. leaving the page drops the images still waiting
  p = await ctx.open({ ...access, media_delay: 0.5,
    poll: [page(Array.from({ length: 8 }, (_, i) => fig('q' + i)).join(''))], pages: { '/page/other.mu': [page('ELSEWHERE')] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForSelector('figure.mu-image', { timeout: 4000 });
  await p.waitForTimeout(300);
  await p.evaluate(u => { navigateTo(u); }, `hash://${T}/page/other.mu`);
  await p.waitForFunction(() => document.getElementById('page-content').innerText.includes('ELSEWHERE'), null, { timeout: 5000 });
  await p.waitForTimeout(300);
  const before = Object.values((await stats(p)).media_calls).reduce((a, b) => a + b, 0);
  await p.waitForTimeout(2500);
  const after = Object.values((await stats(p)).media_calls).reduce((a, b) => a + b, 0);
  ctx.check(after === before && after < 8, `images still queued are not fetched after leaving (${before} -> ${after} of 8)`);

  // 7. images inside a partial load too
  const part = `<a class="mu-dynamic" href="/page?url=hash%3A%2F%2F${T}%2Fpage%2Fbox.mu" data-pid="box">[live]</a>`;
  p = await ctx.open({ ...access, poll: [page(part)], pages: { '/page/box.mu': [page(fig('inbox'))] } });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await loaded(p, 'inbox');
  ctx.check(true, 'an image inside partial content is loaded as well');

  // 8. a request is only ever built for a well-formed node and /media/ path
  p = await ctx.open({ ...access, poll: [page(
    `<img class="mu-image-el" data-media-hash="${T}" data-media-path="/media/../x.webp">` +
    `<img class="mu-image-el" data-media-hash="nothex" data-media-path="/media/a.webp">` +
    `<img class="mu-image-el" data-media-hash="${T}" data-media-path="/page/a.webp">`)] });
  await p.evaluate(u => { navigateTo(u); }, ROOT);
  await p.waitForSelector('img.mu-image-el', { state: 'attached', timeout: 4000 });
  await p.waitForTimeout(800);
  const s8 = await stats(p);
  ctx.check(Object.keys(s8.media_calls).length === 0, 'malformed image targets are never requested');
};
