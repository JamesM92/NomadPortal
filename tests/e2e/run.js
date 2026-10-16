// Runs every suite in ./suites against one mock backend and one browser.
//   cd tests/e2e && npm ci && npx playwright install chromium && node run.js
// Optional: E2E_CHROMIUM=/path/to/chrome, E2E_PYTHON=/path/to/python3,
//           node run.js refresh   (run only suites whose name contains "refresh")
const fs = require('fs');
const path = require('path');
const { start } = require('./lib');

(async () => {
  const only = process.argv[2];
  const files = fs.readdirSync(path.join(__dirname, 'suites')).filter(f => f.endsWith('.js')).sort()
    .filter(f => !only || f.includes(only));
  const ctx = await start();
  try {
    for (const f of files) {
      console.log(`\n${f.replace(/\.js$/, '')}`);
      await require(path.join(__dirname, 'suites', f))(ctx);
    }
  } finally {
    await ctx.close();
  }
  const total = ctx.results.length;
  console.log(ctx.failures.length
    ? `\n${ctx.failures.length} of ${total} checks FAILED`
    : `\nall ${total} browser checks passed`);
  process.exit(ctx.failures.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
