'use strict';
// The browser of a sandbox, for a step that looks at a project's running application.
//
//   const { launch } = require('sandbox-browser');
//   const browser = await launch();
//
// It is Playwright's driver with the image's own Chromium, set up for where it runs: the
// pod is the sandbox, so Chromium's own is off; there is no shared memory to speak of;
// and the only way out is the platform's proxy, which opens a tunnel to the
// application's host when the browser presents the grant the step was given.
const { chromium } = require('playwright-core');

const EXECUTABLE = process.env.SANDBOX_BROWSER || '/usr/bin/chromium';

function wayOut() {
  const server = process.env.HTTPS_PROXY || process.env.https_proxy;
  if (!server) return undefined;
  const grant = process.env.SANDBOX_BROWSER_GRANT;
  return grant ? { server, username: 'grant', password: grant } : { server };
}

/** Start the browser. `options` are Playwright's launch options, added to the sandbox's own. */
async function launch(options = {}) {
  return chromium.launch({
    headless: true,
    ...options,
    executablePath: EXECUTABLE,
    proxy: wayOut(),
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', ...(options.args || [])],
  });
}

module.exports = { launch, chromium };
