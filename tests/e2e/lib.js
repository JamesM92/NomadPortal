// Shared harness: start the mock backend on a free port, launch Chromium,
// and give each suite a few helpers. Suites report results through
// ctx.check(); a page error (uncaught exception in app.js) is a failure.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); });
    s.on('error', reject);
  });
}

function waitForServer(base, tries = 60) {
  return new Promise((resolve, reject) => {
    const attempt = n => http.get(base + '/__stats', res => { res.resume(); resolve(); })
      .on('error', () => (n <= 0 ? reject(new Error('mock backend did not start')) : setTimeout(() => attempt(n - 1), 250)));
    attempt(tries);
  });
}

async function start() {
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(process.env.E2E_PYTHON || 'python3',
    [path.join(__dirname, 'server.py'), String(port)], { stdio: ['ignore', 'ignore', 'inherit'] });
  await waitForServer(base);
  const launch = { args: ['--no-sandbox'] };
  if (process.env.E2E_CHROMIUM) launch.executablePath = process.env.E2E_CHROMIUM;
  const browser = await chromium.launch(launch);

  const failures = [];
  const results = [];
  const opened = [];
  const ctx = {
    base, browser,
    check(ok, msg) {
      results.push({ ok: !!ok, msg });
      console.log(ok ? '  ok  ' : '  FAIL', msg);
      if (!ok) failures.push(msg);
    },
    async scenario(page, body) {
      await page.evaluate(b => fetch('/__scenario', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }), body);
    },
    // A fresh page with the scenario applied, loaded and ready to navigate.
    // Dialogs (the external-site warning) are accepted unless acceptDialogs:false.
    async open(scn, opts = {}) {
      // One test page at a time: a page left open keeps running its timers
      // against the shared mock backend and would skew later counts.
      for (const old of opened.splice(0)) await old.close().catch(() => {});
      const context = await browser.newContext();
      opened.push(context);
      const page = await context.newPage();
      page.dialogs = [];
      page.on('dialog', d => { page.dialogs.push(d.type()); opts.acceptDialogs === false ? d.dismiss() : d.accept(); });
      page.on('pageerror', e => ctx.check(false, `uncaught page error: ${e.message}`));
      await page.goto(base + '/');
      await ctx.scenario(page, { cached: null, delay: 0, ...scn });
      await page.reload();
      await page.waitForFunction(() => typeof navigateTo === 'function');
      if (opts.afterLoad) await page.evaluate(opts.afterLoad);
      await page.waitForTimeout(300);
      return page;
    },
    // A real admin page (the genuine templates, routes and scripts; only
    // Reticulum is faked). `scn.admin_role` is "admin" (default), "user"
    // or "anon". Failed same-origin responses and console errors are kept
    // on page.bad for the suite to judge.
    async openAdmin(scn, route) {
      for (const old of opened.splice(0)) await old.close().catch(() => {});
      const context = await browser.newContext();
      opened.push(context);
      const page = await context.newPage();
      page.bad = [];
      page.on('pageerror', e => ctx.check(false, `uncaught page error: ${e.message}`));
      page.on('response', r => { if (r.url().startsWith(base) && r.status() >= 400 && !r.url().includes('favicon')) page.bad.push(`${r.status()} ${r.url().replace(base, '')}`); });
      page.on('console', m => { if (m.type() === 'error' && !/status of (4|5)\d\d/.test(m.text())) page.bad.push(`console: ${m.text()}`); });
      await page.goto(base + '/__stats');
      await ctx.scenario(page, scn);
      const resp = await page.goto(base + route);
      page.status = resp.status();
      return page;
    },
    text: page => page.evaluate(() => document.getElementById('page-content').innerText),
    fetches: page => page.evaluate(() => fetch('/__stats').then(r => r.json()).then(j => j.fetches)),
  };
  ctx.close = async () => { await browser.close(); server.kill(); };
  ctx.failures = failures;
  ctx.results = results;
  return ctx;
}

module.exports = { start };
