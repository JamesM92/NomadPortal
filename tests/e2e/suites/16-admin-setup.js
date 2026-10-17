// First-run admin setup and sign-in in Chromium: with no admin account the
// sign-in page leads to a setup page that needs the one-time code (read from
// a private file, never shown on a page); wrong codes and weak passwords are
// explained; the right code creates the account and signs in; afterwards
// setup is closed and the normal sign-in works. With ADMIN_PASSWORD set there
// is no setup at all.
const GOOD = 'correct horse battery';
const wait = (p, fn, arg) => p.waitForFunction(fn, arg, { timeout: 4000 });
const codeOf = p => p.evaluate(() => fetch('/__setup_code').then(r => r.json()).then(j => j.code));
const err = p => p.locator('.login-error').innerText().catch(() => '');

async function fill(p, o) {
  await p.fill('#code', o.code);
  await p.fill('#username', o.username ?? 'boss');
  await p.fill('#password', o.password ?? GOOD);
  await p.fill('#confirm', o.confirm ?? o.password ?? GOOD);
}
async function submit(p) {
  await Promise.all([p.waitForNavigation({ waitUntil: 'load' }), p.click('button.btn-login')]);
}

module.exports = async ctx => {
  let p = await ctx.openAdmin({ admin_role: 'real' }, '/auth/login');
  ctx.check(/\/auth\/setup$/.test(p.url()), 'with no admin account the sign-in page leads to setup');
  ctx.check(await p.locator('#code').isVisible() && await p.locator('#confirm').isVisible(), 'setup asks for the code and the password twice');
  const code = await codeOf(p);
  ctx.check(!!code && /^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$/.test(code), 'the code is in the private file, in a form that is easy to type');
  ctx.check(!(await p.content()).includes(code), 'the code is not on the page');
  ctx.check(/admin_setup_code/.test(await p.innerText('body')), 'the page says where to find the code');
  ctx.check(/reset-password/.test(await p.innerText('body')), 'and how a forgotten password is recovered (no email reset)');

  // a wrong code
  await fill(p, { code: 'AAAA-AAAA-AAAA-AAAA' });
  await submit(p);
  ctx.check(/not right/.test(await err(p)) && /\/auth\/setup$/.test(p.url()), 'a wrong code is explained and setup stays open');

  // mismatched, then weak
  await fill(p, { code, confirm: 'something else entirely' });
  await submit(p);
  ctx.check(/do not match/.test(await err(p)), 'passwords that differ are explained');
  await p.evaluate(() => { for (const id of ['password', 'confirm']) { const e = document.getElementById(id); e.minLength = 0; } });
  await fill(p, { code, password: 'short', confirm: 'short' });
  await p.evaluate(() => { for (const id of ['password', 'confirm']) document.getElementById(id).removeAttribute('minlength'); });
  await submit(p);
  ctx.check(/at least 12/.test(await err(p)), 'a short password is explained (the server checks even when the browser does not)');
  ctx.check(await codeOf(p) === code, 'and the code is still valid after those mistakes');

  // the right code
  await fill(p, { code: code.toLowerCase().replace(/-/g, ' ') });
  await submit(p);
  ctx.check(/\/admin\/$/.test(p.url()), 'the right code (typed loosely) creates the account and signs in to the dashboard');
  ctx.check(await p.locator('.admin-user').innerText().then(t => /boss/.test(t)), 'signed in as the new admin');
  ctx.check(await codeOf(p) === null, 'the code file is gone');

  // setup is closed
  const closed = await ctx.openAdmin({ admin_role: 'real', admin_account: { username: 'boss', password: GOOD } }, '/auth/setup');
  ctx.check(/\/auth\/login$/.test(closed.url()), 'once an account exists the setup page is closed');
  ctx.check(await closed.locator('#username').isVisible() && await closed.locator('#code').count() === 0, 'and the normal sign-in is shown');

  // normal sign-in with the stored account
  await closed.fill('#username', 'boss');
  await closed.fill('#password', 'not the password');
  await submit(closed);
  ctx.check(/Invalid username or password/.test(await err(closed)), 'a wrong password is refused');
  await closed.fill('#username', 'boss');
  await closed.fill('#password', GOOD);
  await submit(closed);
  ctx.check(/\/admin\/$/.test(closed.url()), 'the right password signs in');
  await closed.goto(ctx.base + '/auth/logout');
  await closed.goto(ctx.base + '/admin/cache');
  ctx.check(/\/auth\/login/.test(closed.url()), 'after signing out the admin area asks for sign-in again');

  // the environment override
  const env = await ctx.openAdmin({ admin_role: 'real', admin_env_password: 'from-the-env' }, '/auth/login');
  ctx.check(/\/auth\/login$/.test(env.url()) && await env.locator('#password').isVisible(), 'with ADMIN_PASSWORD set the sign-in page is shown directly');
  await env.goto(ctx.base + '/auth/setup');
  ctx.check(/\/auth\/login$/.test(env.url()), 'and setup is not offered');
  await env.fill('#username', 'root');
  await env.fill('#password', 'from-the-env');
  await submit(env);
  ctx.check(/\/admin\/$/.test(env.url()), 'the variable still signs in as before');
};
